"""Executor-side mutex queue: drain (issue #68 PR #2) + scheduled
admission (issue #696).

When a run finalises in a job carrying ``mutex_group``, the executor
pops the next queued sibling entry and spawns it detached. We mock the
spawn so no real subprocess runs and assert the spawn argv.

``TestExecutorScheduledAdmission`` covers the other half: a
schtasks-fired run whose group is already held must land in the queue
with ``status="queued"`` instead of running concurrently, while every
already-admitted path (manual/webhook/API at the route, chain fires, and
the drain's own replay) must still run straight through.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.commands import run_job_cmd as rjc
from src import jobs as jobs_mod
from src import jobs_history as jobs_history_mod
from src import jobs_queue as jobs_queue_mod
from src.app_config import AppConfig
from src.jobs_config import Job, JobsConfig


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    # JOBS_RUNS_DIR / JOBS_QUEUE_PATH are owned by src.jobs_history /
    # src.jobs_queue respectively (issue #315 split).
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_queue_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    return tmp_path


def _seed_run(runs_root, job_id, run_id, *, started_at, status):
    rd = runs_root / job_id / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(
        json.dumps({
            "run_id": run_id,
            "job_id": job_id,
            "status": status,
            "started_at": started_at,
        }),
        encoding="utf-8",
    )
    return rd


def _silence_notifier(monkeypatch):
    monkeypatch.setattr(
        rjc, "load_webapp_config",
        lambda: SimpleNamespace(
            notify_on_failure=False,
            notify_failure_summary=False,
            notify_failure_streak=0,
        ),
    )


class TestExecutorMutexDrain:
    def test_finalising_executor_spawns_next_queued(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """A successful run finalisation in mutex_group X pops the head
        queued entry for X and spawns the run via the detached spawn."""
        # A short python script that exits 0.
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        job_a = Job(
            id="alpha", name="Alpha", script_path=str(script),
            mutex_group="chrome",
        )
        # Pre-seed the queue with a sibling entry (job beta, run rN).
        # The drainer expects the run dir to exist with status=queued.
        _seed_run(
            isolated_jobs, "beta", "20260101T000010",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="queued",
        )
        jobs_mod.enqueue_mutex("chrome", {
            "job_id": "beta",
            "run_id": "20260101T000010",
            "trigger": "manual",
            "params": None,
        })
        # Job registry — alpha is the one we're running, beta exists so
        # the route logic / drainer can find it; the drainer ultimately
        # spawns by id without consulting the registry, so this is just
        # for completeness.
        monkeypatch.setattr(
            "src.jobs_config.load_jobs",
            lambda: JobsConfig(jobs=[job_a]),
        )
        monkeypatch.setattr(rjc, "load_jobs", lambda: JobsConfig(jobs=[job_a]))
        _silence_notifier(monkeypatch)
        spawn = MagicMock(return_value=99999)
        monkeypatch.setattr(jobs_queue_mod, "spawn_run_job_detached", spawn)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="alpha", trigger="manual", run_id=None, params=None,
        ))
        assert rc == 0
        # Spawn was called for beta's queued run, not alpha's.
        assert spawn.called
        args = spawn.call_args.args
        assert args[0] == "beta"
        assert args[1] == "20260101T000010"
        # The queue is now empty.
        assert jobs_mod.peek_mutex_queue("chrome") == []

    def test_drain_skips_non_queued_entry(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """If the head's run dir was already promoted (status=running or
        success) the drainer must NOT spawn — that's the double-spawn
        guard. The head is still popped (the queue moves forward)."""
        # The head's record is already 'running' (someone else picked it up).
        _seed_run(
            isolated_jobs, "beta", "20260101T000010",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="running",
        )
        jobs_mod.enqueue_mutex("chrome", {
            "job_id": "beta", "run_id": "20260101T000010",
            "trigger": "manual",
        })
        spawn = MagicMock()
        result = jobs_mod.drain_mutex_queue("chrome", spawn=spawn)
        assert result is None
        assert not spawn.called
        # Queue advanced past the malformed head (idempotency over
        # forever-stuck queues).
        assert jobs_mod.peek_mutex_queue("chrome") == []


class TestExecutorScheduledAdmission:
    """Scheduled (schtasks) fires honour ``mutex_group`` (issue #696).

    Before this, ``mutex_group`` was enforced only on the webapp admission
    path, so the weekly fleet chain — the primary reason the feature
    exists — ran unserialized.
    """

    @staticmethod
    def _stub_load_jobs(monkeypatch, jobs):
        cfg = JobsConfig(jobs=list(jobs))
        monkeypatch.setattr("src.jobs_config.load_jobs", lambda: cfg)
        monkeypatch.setattr(rjc, "load_jobs", lambda: cfg)

    @staticmethod
    def _beta_runs(runs_root):
        """Beta's run dirs, newest last."""
        beta_dir = runs_root / "beta"
        return sorted(beta_dir.iterdir()) if beta_dir.is_dir() else []

    @staticmethod
    def _record(run_dir):
        return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    def _pair(self, tmp_path):
        """alpha (the group holder) + beta (the job being fired)."""
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        alpha = Job(
            id="alpha", name="Alpha", script_path=str(script),
            mutex_group="fleet-weekly",
        )
        beta = Job(
            id="beta", name="Beta", script_path=str(script),
            mutex_group="fleet-weekly",
        )
        return alpha, beta

    def test_scheduled_fire_behind_running_sibling_is_queued(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        alpha, beta = self._pair(tmp_path)
        # Alpha holds the group. No pid on the record, so the reaper in
        # mutex_collision leaves it alone (src/jobs_reap.py::_reap_one).
        _seed_run(
            isolated_jobs, "alpha", "20260101T000000",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="running",
        )
        self._stub_load_jobs(monkeypatch, [alpha, beta])
        _silence_notifier(monkeypatch)
        popen = MagicMock(
            side_effect=AssertionError("queued fire must not spawn a child")
        )
        monkeypatch.setattr(rjc.subprocess, "Popen", popen)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="beta", trigger="scheduled", run_id=None, params=None,
        ))

        assert rc == 0
        assert not popen.called
        runs = self._beta_runs(isolated_jobs)
        assert len(runs) == 1
        record = self._record(runs[0])
        assert record["status"] == "queued"
        assert record["mutex_group"] == "fleet-weekly"
        assert record["mutex_blocked_by"] == "alpha"
        assert record["trigger"] == "scheduled"
        assert record["trigger_source"] == "schtasks"
        # No output.log — nothing ran.
        assert not (runs[0] / "output.log").exists()
        # And the fire is parked in the queue for alpha's finaliser to drain.
        assert jobs_mod.peek_mutex_queue("fleet-weekly") == [
            {
                "job_id": "beta",
                "run_id": runs[0].name,
                "trigger": "scheduled",
                "params": None,
            }
        ]

    def test_scheduled_fire_with_free_group_runs(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        alpha, beta = self._pair(tmp_path)
        # Alpha finished — the group is free.
        _seed_run(
            isolated_jobs, "alpha", "20260101T000000",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="success",
        )
        self._stub_load_jobs(monkeypatch, [alpha, beta])
        _silence_notifier(monkeypatch)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="beta", trigger="scheduled", run_id=None, params=None,
        ))

        assert rc == 0
        runs = self._beta_runs(isolated_jobs)
        assert len(runs) == 1
        assert self._record(runs[0])["status"] == "success"
        assert jobs_mod.peek_mutex_queue("fleet-weekly") == []

    def test_manual_fire_is_not_double_gated(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """The route already ran ``mutex_collision`` for manual / webhook /
        API fires; a second gate here would re-queue a fire the caller was
        told would run. Same rule as the cooldown gate.
        """
        alpha, beta = self._pair(tmp_path)
        _seed_run(
            isolated_jobs, "alpha", "20260101T000000",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="running",
        )
        self._stub_load_jobs(monkeypatch, [alpha, beta])
        _silence_notifier(monkeypatch)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="beta", trigger="manual", run_id=None, params=None,
        ))

        assert rc == 0
        runs = self._beta_runs(isolated_jobs)
        assert self._record(runs[-1])["status"] == "success"
        assert jobs_mod.peek_mutex_queue("fleet-weekly") == []

    def test_drained_scheduled_replay_is_not_requeued(
        self, isolated_jobs, tmp_path, monkeypatch
    ):
        """``drain_mutex_queue`` replays the entry's original
        ``trigger="scheduled"`` with a pre-created ``--run-id``. That fire
        has already been admitted, so it must run — not get pushed back
        onto the tail of the queue it was just released from.
        """
        alpha, beta = self._pair(tmp_path)
        # Alpha still reads "running" (its finaliser drains before its own
        # record is re-read by anyone), which is the worst case for a
        # re-entrant gate.
        _seed_run(
            isolated_jobs, "alpha", "20260101T000000",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="running",
        )
        queued = _seed_run(
            isolated_jobs, "beta", "20260101T000010",
            started_at=datetime.now().isoformat(timespec="seconds"),
            status="queued",
        )
        self._stub_load_jobs(monkeypatch, [alpha, beta])
        _silence_notifier(monkeypatch)

        cmd = rjc.RunJobCommand(AppConfig())
        rc = cmd.execute(SimpleNamespace(
            job_id="beta", trigger="scheduled", run_id=queued.name,
            params=None,
        ))

        assert rc == 0
        assert self._record(queued)["status"] == "success"
        assert self._beta_runs(isolated_jobs) == [queued]
        assert jobs_mod.peek_mutex_queue("fleet-weekly") == []
