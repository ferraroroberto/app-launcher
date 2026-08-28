"""The one mutex-admission implementation (issue #794, finding 2).

``src.jobs_queue.admit_and_spawn`` replaced three parallel copies of the
same admission block — the webapp route's ``_admit_and_spawn``, the chain
dispatcher's ``dispatch_chain_run``, and the executor's own
``_finalize_mutex_queue``. They had already drifted once (a chain-spawn
``OSError`` wrote ``status="failed"`` to disk while returning a meta still
claiming ``"pending"``), so these tests pin the contract at the shared
helper rather than three times over:

* the queued record has the same shape whichever path produced it;
* a spawn ``OSError`` leaves one status, on disk *and* in the return value,
  carrying the reason;
* ``spawn_when_free=False`` (the executor's half) touches nothing at all
  when the group is free, so the caller runs the job inline.

The route-level test at the bottom is the regression proof for the one
behaviour this refactor changed: a webapp spawn failure now records *why*
on the run, not just an unexplained ``failed``.
"""

from __future__ import annotations

import json

import pytest

from src import jobs_history as jobs_history_mod
from src import jobs_queue as jobs_queue_mod
from src.jobs_config import Job

from tests.test_webapp_api_jobs import _seed_one_job


@pytest.fixture
def stubbed_schtasks(monkeypatch):
    """Minimal schtasks stubs so job CRUD works without Task Scheduler
    (the run path itself is stubbed per-test)."""
    from unittest.mock import MagicMock
    from app.webapp.routers import jobs as jobs_router

    for name in ("sync_schtasks", "delete_schtasks"):
        monkeypatch.setattr(jobs_router.jobs_mod, name, MagicMock(return_value=[]))
    monkeypatch.setattr(
        jobs_router.jobs_mod, "query_next_run", MagicMock(return_value=None)
    )


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_queue_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    return tmp_path


def _seed_run(runs_root, job_id, run_id, *, status):
    rd = runs_root / job_id / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(
        json.dumps({"run_id": run_id, "job_id": job_id, "status": status}),
        encoding="utf-8",
    )
    return rd


def _record(runs_root, meta):
    path = runs_root / meta["job_id"] / meta["run_id"] / "run.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestAdmitAndSpawnFreeGroup:
    def test_spawns_and_returns_pending(self, isolated_jobs, monkeypatch):
        calls = []
        monkeypatch.setattr(
            jobs_queue_mod,
            "spawn_run_job_detached",
            lambda *a, **kw: calls.append(a) or 1234,
        )
        job = Job(id="solo", name="Solo", script_path="C:\\x.py")

        meta = jobs_queue_mod.admit_and_spawn([job], job, "manual")

        assert meta is not None
        assert meta["status"] == "pending"
        assert _record(isolated_jobs, meta)["status"] == "pending"
        # (job_id, run_id, trigger, params)
        assert calls == [("solo", meta["run_id"], "manual", None)]

    def test_spawn_failure_has_one_status_and_a_reason(
        self, isolated_jobs, monkeypatch
    ):
        """The drift class this helper exists to make impossible: the
        returned meta and the on-disk record must agree, and the record
        must say *why* it failed."""

        def _boom(*_a, **_kw):
            raise OSError("CreateProcess failed")

        monkeypatch.setattr(jobs_queue_mod, "spawn_run_job_detached", _boom)
        job = Job(id="solo", name="Solo", script_path="C:\\x.py")

        meta = jobs_queue_mod.admit_and_spawn([job], job, "manual")

        assert meta is not None
        assert meta["status"] == "failed"
        assert meta["exit_code"] == -1
        assert "CreateProcess failed" in meta["spawn_error"]
        record = _record(isolated_jobs, meta)
        assert record["status"] == meta["status"]
        assert record["exit_code"] == -1
        assert "CreateProcess failed" in record["spawn_error"]

    def test_spawn_error_key_is_caller_chosen(self, isolated_jobs, monkeypatch):
        """Chain fires keep their own ``chain_spawn_error`` key, which the
        Jobs history already carries."""

        def _boom(*_a, **_kw):
            raise OSError("nope")

        monkeypatch.setattr(jobs_queue_mod, "spawn_run_job_detached", _boom)
        job = Job(id="solo", name="Solo", script_path="C:\\x.py")

        meta = jobs_queue_mod.admit_and_spawn(
            [job], job, "chain:up", spawn_error_key="chain_spawn_error"
        )

        assert meta is not None
        assert "chain_spawn_error" in meta
        assert "spawn_error" not in meta


class TestAdmitAndSpawnCollision:
    def test_queues_behind_holder_without_spawning(
        self, isolated_jobs, monkeypatch
    ):
        spawned = []
        monkeypatch.setattr(
            jobs_queue_mod,
            "spawn_run_job_detached",
            lambda *a, **kw: spawned.append(a),
        )
        holder = Job(id="held", name="Held", script_path="C:\\a.py", mutex_group="g")
        job = Job(id="mine", name="Mine", script_path="C:\\b.py", mutex_group="g")
        _seed_run(isolated_jobs, "held", "run-held", status="running")

        meta = jobs_queue_mod.admit_and_spawn([holder, job], job, "manual")

        assert meta is not None
        assert meta["status"] == "queued"
        assert meta["mutex_group"] == "g"
        assert meta["mutex_blocked_by"] == "held"
        assert spawned == []
        record = _record(isolated_jobs, meta)
        assert record["status"] == "queued"
        assert record["mutex_blocked_by"] == "held"
        entries = jobs_queue_mod.peek_mutex_queue("g")
        assert [e["run_id"] for e in entries] == [meta["run_id"]]

    def test_extra_meta_and_params_land_on_the_record(
        self, isolated_jobs, monkeypatch
    ):
        monkeypatch.setattr(
            jobs_queue_mod, "spawn_run_job_detached", lambda *a, **kw: 1
        )
        holder = Job(id="held", name="Held", script_path="C:\\a.py", mutex_group="g")
        job = Job(id="mine", name="Mine", script_path="C:\\b.py", mutex_group="g")
        _seed_run(isolated_jobs, "held", "run-held", status="running")

        meta = jobs_queue_mod.admit_and_spawn(
            [holder, job],
            job,
            "scheduled",
            params={"who": "world"},
            extra_meta={"trigger_source": "schtasks"},
            spawn_when_free=False,
        )

        assert meta is not None
        record = _record(isolated_jobs, meta)
        assert record["trigger_source"] == "schtasks"
        assert record["params"] == {"who": "world"}
        assert record["trigger"] == "scheduled"
        entries = jobs_queue_mod.peek_mutex_queue("g")
        assert [e["params"] for e in entries] == [{"who": "world"}]


class TestAdmitAndSpawnQueueOnlyMode:
    def test_free_group_touches_nothing(self, isolated_jobs, monkeypatch):
        """The executor's half: no collision means "proceed inline" — no
        run dir, no record, no spawn."""
        spawned = []
        monkeypatch.setattr(
            jobs_queue_mod,
            "spawn_run_job_detached",
            lambda *a, **kw: spawned.append(a),
        )
        job = Job(id="solo", name="Solo", script_path="C:\\x.py", mutex_group="g")

        meta = jobs_queue_mod.admit_and_spawn(
            [job], job, "scheduled", spawn_when_free=False
        )

        assert meta is None
        assert spawned == []
        assert not (isolated_jobs / "solo").exists()


class TestOnRunDirCallback:
    def test_fires_before_the_queued_branch(self, isolated_jobs, monkeypatch):
        """The webhook route persists ``_webhook.json`` next to ``run.json``
        regardless of which branch the fire takes."""
        monkeypatch.setattr(
            jobs_queue_mod, "spawn_run_job_detached", lambda *a, **kw: 1
        )
        seen = []
        holder = Job(id="held", name="Held", script_path="C:\\a.py", mutex_group="g")
        job = Job(id="mine", name="Mine", script_path="C:\\b.py", mutex_group="g")
        _seed_run(isolated_jobs, "held", "run-held", status="running")

        meta = jobs_queue_mod.admit_and_spawn(
            [holder, job], job, "webhook", on_run_dir=seen.append
        )

        assert meta is not None
        assert [p.name for p in seen] == [meta["run_id"]]


class TestRouteSurfacesSpawnFailure:
    """Regression (issue #794): a spawn ``OSError`` on the webapp path used
    to leave a bare ``failed`` record with no reason recorded anywhere — the
    message existed only in the HTTP 500 the caller saw and then dropped.
    Now the shared helper writes it onto the run like the chain path always
    did."""

    def test_500_and_a_recorded_reason(
        self, webapp_client, stubbed_schtasks, monkeypatch
    ):
        client, _tmp, _ = webapp_client
        created = _seed_one_job(client, name="Boom").json()["job"]

        def _boom(*_a, **_kw):
            raise OSError("CreateProcess failed")

        monkeypatch.setattr(jobs_queue_mod, "spawn_run_job_detached", _boom)

        resp = client.post("/api/jobs/" + created["id"] + "/run")

        assert resp.status_code == 500
        assert "spawn failed: CreateProcess failed" in resp.json()["detail"]

        runs_root = jobs_history_mod.JOBS_RUNS_DIR / created["id"]
        records = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in runs_root.glob("*/run.json")
        ]
        assert len(records) == 1
        assert records[0]["status"] == "failed"
        assert records[0]["exit_code"] == -1
        assert "CreateProcess failed" in records[0]["spawn_error"]
