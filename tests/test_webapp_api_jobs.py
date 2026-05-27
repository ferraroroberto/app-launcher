"""/api/jobs surface — list, CRUD, run, history (issue #47).

Schtasks and the detached executor spawn are mocked at the router-module
level so no real `schtasks.exe` runs and no subprocess is left behind.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


def _seed_one_job(client, name="Demo", script="C:\\stub\\demo.bat",
                  schedule=None, args=""):
    payload = {
        "name": name,
        "script_path": script,
        "args": args,
        "schedule": schedule or {"type": "none"},
    }
    return client.post("/api/jobs", json=payload)


@pytest.fixture
def mocked_jobs_side_effects(monkeypatch):
    """Stub schtasks I/O so the router can run without Windows Task Scheduler.

    Returns a dict of MagicMocks tests can inspect for call args.
    """
    from unittest.mock import MagicMock
    from app.webapp.routers import jobs as jobs_router

    mocks = {
        "sync_schtasks": MagicMock(return_value=[]),
        "delete_schtasks": MagicMock(return_value=[]),
        "query_next_run": MagicMock(return_value=None),
        "spawn_run_job_detached": MagicMock(return_value=1234),
        # Issue #66 — decorate_job now calls run_stats + is_stuck for
        # every row; default to "no data" so existing assertions remain
        # untouched and new behaviour is opt-in per test.
        "run_stats": MagicMock(
            return_value={
                "p50": None,
                "p95": None,
                "success_rate_30d": None,
                "completed_count": 0,
                "last7": [],
            }
        ),
        "is_stuck": MagicMock(return_value=False),
    }
    for name, m in mocks.items():
        monkeypatch.setattr(jobs_router.jobs_mod, name, m)
    return mocks


# =================================================================== CRUD


class TestCreateJob:
    def test_minimal_create(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = _seed_one_job(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["job"]["name"] == "Demo"
        # Default schedule chip for type=none is empty string.
        assert body["job"]["schedule_chip"] == ""
        # Schtasks sync was called for the new job.
        assert mocked_jobs_side_effects["sync_schtasks"].called

    def test_missing_name_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={"name": "", "script_path": "C:\\stub\\demo.bat"},
        )
        assert resp.status_code == 400

    def test_missing_script_path_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs", json={"name": "Demo"})
        assert resp.status_code == 400

    def test_bad_suffix_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={"name": "X", "script_path": "C:\\stub\\x.txt"},
        )
        assert resp.status_code == 400

    def test_daily_times_round_trips(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = _seed_one_job(
            client,
            schedule={"type": "daily_times", "at": ["06:00", "12:00", "18:00"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["job"]["schedule"]["type"] == "daily_times"
        assert body["job"]["schedule"]["at"] == ["06:00", "12:00", "18:00"]

    def test_duplicate_id_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        first = _seed_one_job(client, name="Demo")
        assert first.status_code == 200
        # Same name → same slug id → 409.
        second = client.post(
            "/api/jobs",
            json={
                "id": first.json()["job"]["id"],
                "name": "Demo",
                "script_path": "C:\\stub\\demo.bat",
            },
        )
        assert second.status_code == 409


class TestListJobs:
    def test_empty_initially(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert resp.json() == {"jobs": []}

    def test_lists_after_create(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        _seed_one_job(client, name="Alpha")
        _seed_one_job(client, name="Bravo")
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        names = [j["name"] for j in resp.json()["jobs"]]
        assert names == ["Alpha", "Bravo"]


class TestEditJob:
    def test_404_on_unknown(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.put("/api/jobs/nope", json={"name": "X"})
        assert resp.status_code == 404

    def test_changes_name_and_resyncs(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        mocked_jobs_side_effects["sync_schtasks"].reset_mock()
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"name": "Renamed", "schedule": {"type": "daily", "at": "07:00"}},
        )
        assert resp.status_code == 200
        assert resp.json()["job"]["name"] == "Renamed"
        # Edits must re-sync schtasks so the schedule change lands.
        assert mocked_jobs_side_effects["sync_schtasks"].called


class TestDeleteJob:
    def test_404_on_unknown(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.delete("/api/jobs/nope")
        assert resp.status_code == 404

    def test_removes_and_deletes_schtasks(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.delete("/api/jobs/" + created["id"])
        assert resp.status_code == 200
        assert resp.json()["removed"] == created["id"]
        # And the schtasks deletion was attempted.
        assert mocked_jobs_side_effects["delete_schtasks"].called


# =================================================================== /run


class TestRunJob:
    def test_404_on_unknown(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs/nope/run")
        assert resp.status_code == 404

    def test_spawns_and_returns_run_id(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post("/api/jobs/" + created["id"] + "/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == created["id"]
        run_id = body["run_id"]
        assert run_id
        # The executor was spawned detached with the same run id.
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        assert spawn.called
        assert spawn.call_args.args[1] == run_id


# ================================================================== params


class TestParamsCRUD:
    """Params (issue #67) round-trip through create + edit."""

    def test_create_accepts_params(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Scrape",
                "script_path": "C:\\stub\\scrape.py",
                "params": [
                    {"name": "since", "kind": "date", "flag": "--since"},
                    {"name": "verbose", "kind": "bool", "flag": "--verbose",
                     "default": False},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        params = resp.json()["job"]["params"]
        assert [p["name"] for p in params] == ["since", "verbose"]

    def test_create_rejects_bad_param_shape(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "X", "script_path": "C:\\stub\\x.py",
                "params": [{"name": "x", "kind": "bogus"}],
            },
        )
        assert resp.status_code == 400

    def test_edit_replaces_params(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"params": [{"name": "n", "kind": "int", "flag": "--n"}]},
        )
        assert resp.status_code == 200
        params = resp.json()["job"]["params"]
        assert params and params[0]["name"] == "n"


class TestRunJobWithParams:
    def _seed_param_job(self, client):
        return client.post(
            "/api/jobs",
            json={
                "name": "Scrape",
                "script_path": "C:\\stub\\scrape.py",
                "params": [
                    {"name": "since", "kind": "date", "flag": "--since"},
                    {"name": "tier", "kind": "enum",
                     "options": ["a", "b"], "default": "a", "flag": "--tier"},
                ],
            },
        ).json()["job"]

    def test_run_with_valid_params(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"since": "2026-06-01", "tier": "b"}},
        )
        assert resp.status_code == 200, resp.text
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        # spawn was passed the validated params payload (4th positional arg).
        assert spawn.call_args.args[3] == {"since": "2026-06-01", "tier": "b"}

    def test_missing_required_returns_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"tier": "a"}},
        )
        assert resp.status_code == 400
        assert "since" in resp.json()["detail"]

    def test_unknown_param_returns_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"since": "2026-06-01", "bogus": 1}},
        )
        assert resp.status_code == 400
        assert "bogus" in resp.json()["detail"]

    def test_enum_not_in_options_returns_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_param_job(client)
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run",
            json={"params": {"since": "2026-06-01", "tier": "c"}},
        )
        assert resp.status_code == 400

    def test_empty_body_still_works_for_parameterless_job(
        self, webapp_client, mocked_jobs_side_effects
    ):
        # Regression: parameter-less jobs must keep their one-tap fire.
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post("/api/jobs/" + created["id"] + "/run")
        assert resp.status_code == 200
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        # 4th positional arg is None (no params payload).
        assert spawn.call_args.args[3] is None


# ================================================================ cooldown


class TestCooldown:
    """``cooldown_seconds`` admission gate on POST /api/jobs/<id>/run.

    Cooldown is measured from the most recent non-skipped run's
    ``started_at``. We seed a real run.json under the job's runs dir
    (the fixture redirects ``JOBS_RUNS_DIR`` to ``tmp_path``) and then
    fire ``/run`` — no executor actually spawns (it's mocked).
    """

    def _seed_run_record(
        self, runs_root, job_id, run_id, *, started_at, status="success"
    ):
        run_dir = runs_root / job_id / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "job_id": job_id,
                    "status": status,
                    "started_at": started_at,
                }
            ),
            encoding="utf-8",
        )

    def test_no_cooldown_allows_back_to_back(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        assert client.post(f"/api/jobs/{created['id']}/run").status_code == 200
        assert client.post(f"/api/jobs/{created['id']}/run").status_code == 200

    def test_inside_window_returns_429(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, overrides = webapp_client
        created = client.post(
            "/api/jobs",
            json={
                "name": "Cool",
                "script_path": "C:\\stub\\x.py",
                "cooldown_seconds": 30,
            },
        ).json()["job"]
        # Anchor — 5 seconds ago, well inside the 30 s window.
        now = datetime.now().replace(microsecond=0)
        anchor_started = (now - timedelta(seconds=5)).isoformat(timespec="seconds")
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"],
            created["id"],
            "20260101T000000",
            started_at=anchor_started,
        )
        resp = client.post(f"/api/jobs/{created['id']}/run")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        retry = int(resp.headers["Retry-After"])
        assert 1 <= retry <= 30
        body = resp.json()
        # FastAPI wraps the structured detail under "detail".
        detail = body["detail"]
        assert detail["detail"] == "cooldown"
        assert detail["cooldown_seconds"] == 30
        assert 1 <= detail["retry_after_seconds"] <= 30
        # Executor must not have been spawned for the rejected fire.
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        assert not spawn.called

    def test_anchor_ignores_skipped_records(
        self, webapp_client, mocked_jobs_side_effects
    ):
        """A `skipped` record sitting on top of an older real run must NOT
        become the cooldown anchor — otherwise rapid mash-fires would keep
        extending the window, turning cooldown into sliding debounce."""
        client, _, overrides = webapp_client
        created = client.post(
            "/api/jobs",
            json={
                "name": "Cool",
                "script_path": "C:\\stub\\x.py",
                "cooldown_seconds": 30,
            },
        ).json()["job"]
        now = datetime.now().replace(microsecond=0)
        # Real run started 60 s ago — outside the 30 s window.
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"],
            created["id"],
            "20260101T000000",
            started_at=(now - timedelta(seconds=60)).isoformat(timespec="seconds"),
            status="success",
        )
        # A skipped record from 5 s ago — must be ignored by the anchor.
        self._seed_run_record(
            overrides["tmp_jobs_runs_dir"],
            created["id"],
            "20260101T000060",
            started_at=(now - timedelta(seconds=5)).isoformat(timespec="seconds"),
            status="skipped",
        )
        resp = client.post(f"/api/jobs/{created['id']}/run")
        assert resp.status_code == 200, resp.text

    def test_cooldown_round_trips_through_put(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"cooldown_seconds": 45},
        )
        assert resp.status_code == 200
        assert resp.json()["job"]["cooldown_seconds"] == 45
        # PUT cooldown_seconds=0 clears it.
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"cooldown_seconds": 0},
        )
        assert resp.status_code == 200
        assert "cooldown_seconds" not in resp.json()["job"]


# ============================================================= run history


class TestRunHistory:
    def test_404_for_unknown_job(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.get("/api/jobs/nope/runs")
        assert resp.status_code == 404

    def test_round_trip_returns_recorded_run(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        # Trigger a run — the route pre-creates the run dir + writes
        # run.json with status=pending.
        run = client.post("/api/jobs/" + created["id"] + "/run").json()
        listing = client.get("/api/jobs/" + created["id"] + "/runs")
        assert listing.status_code == 200
        runs = listing.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == run["run_id"]
        # And the single-run endpoint includes the (empty) output tail.
        single = client.get(
            "/api/jobs/" + created["id"] + "/runs/" + run["run_id"]
        )
        assert single.status_code == 200
        record = single.json()["run"]
        assert record["status"] == "pending"
        assert record.get("output_tail") == ""

    def test_unknown_run_id_returns_404(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.get("/api/jobs/" + created["id"] + "/runs/nope")
        assert resp.status_code == 404


# ====================================================== bulk-cache schtasks


class TestBulkCacheNextRun:
    """`GET /api/jobs` must make at most one schtasks call per cache window
    regardless of job count — the issue-#66 perf prerequisite. The whole
    Jobs tab v1 was N+1 fork+exec on Windows.
    """

    def test_one_schtasks_call_per_window_for_n_jobs(
        self, webapp_client, monkeypatch
    ):
        # We don't want the router-level query_next_run mock here — we
        # want to exercise the real src.jobs query_next_run via the
        # cache. The fixture above mocks `query_next_run` directly on
        # jobs_mod, so re-import the *un*-mocked routine.
        from unittest.mock import MagicMock

        from app.webapp.routers import jobs as jobs_router
        from src import jobs as jobs_mod

        client, _, _ = webapp_client

        # Stub only the schtasks side-effects (sync/delete/spawn) and
        # the stats helpers. Leave query_next_run pointing at the real
        # cached implementation.
        monkeypatch.setattr(jobs_router.jobs_mod, "sync_schtasks", MagicMock(return_value=[]))
        monkeypatch.setattr(jobs_router.jobs_mod, "delete_schtasks", MagicMock(return_value=[]))
        monkeypatch.setattr(
            jobs_router.jobs_mod,
            "run_stats",
            MagicMock(
                return_value={
                    "p50": None, "p95": None, "success_rate_30d": None,
                    "completed_count": 0, "last7": [],
                }
            ),
        )
        monkeypatch.setattr(jobs_router.jobs_mod, "is_stuck", MagicMock(return_value=False))

        # Reset the module-level cache so this test starts fresh.
        jobs_mod.invalidate_next_run_cache()

        # Count schtasks invocations.
        calls: List[List[str]] = []

        def fake_run(argv):
            import subprocess
            calls.append(list(argv))
            # Return one fake task per registered job so the cache
            # actually has entries to look up.
            stdout = (
                "TaskName: \\AppLauncher\\demo-0\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-1\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-2\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-3\nNext Run Time: 2026-06-01 06:00:00\n\n"
                "TaskName: \\AppLauncher\\demo-4\nNext Run Time: 2026-06-01 06:00:00\n\n"
            )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(jobs_mod, "_run_schtasks", fake_run)

        # Seed N=5 jobs.
        for i in range(5):
            client.post(
                "/api/jobs",
                json={"name": f"demo-{i}", "script_path": f"C:\\stub\\d{i}.bat"},
            )
        # Drop any schtasks calls made by create (those go through
        # sync_schtasks which is mocked).
        calls.clear()
        # Cache might have been populated/dirtied by a sync; reset.
        jobs_mod.invalidate_next_run_cache()

        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert len(resp.json()["jobs"]) == 5
        # First call populates the cache → exactly one schtasks shell-out
        # for all five jobs.
        first_window = list(calls)
        assert len(first_window) == 1
        # A second call within the TTL must not hit schtasks again.
        client.get("/api/jobs")
        assert calls == first_window

    def test_invalidation_after_sync(self, monkeypatch):
        from unittest.mock import MagicMock

        from src import jobs as jobs_mod
        from src.jobs_config import Job, Schedule

        # Set up a fake runner that records calls.
        calls = []

        def fake_run(argv):
            import subprocess
            calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(jobs_mod, "_run_schtasks", fake_run)
        jobs_mod.invalidate_next_run_cache()

        # Warm the cache.
        jobs_mod.query_next_run("demo")
        assert len(calls) == 1
        # Second call within TTL → still 1.
        jobs_mod.query_next_run("demo")
        assert len(calls) == 1
        # Invalidate (what sync_schtasks does) → next call re-shells.
        jobs_mod.invalidate_next_run_cache()
        jobs_mod.query_next_run("demo")
        assert len(calls) == 2


# ============================================================= kill route


class TestKillRun:
    def test_404_on_unknown_job(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs/nope/runs/some-rid/kill")
        assert resp.status_code == 404

    def test_404_on_unknown_run(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post(
            "/api/jobs/" + created["id"] + "/runs/no-such-run/kill"
        )
        assert resp.status_code == 404

    def test_409_when_run_already_final(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        # Pre-create a run dir with status=success — not killable.
        from src import jobs as jobs_mod
        run_dir = jobs_mod.new_run_dir(created["id"], "20260524T080000")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="success",
            started_at="2026-05-24T08:00:00",
            finished_at="2026-05-24T08:00:05",
        )
        resp = client.post(
            "/api/jobs/" + created["id"] + "/runs/" + run_dir.name + "/kill"
        )
        assert resp.status_code == 409

    def test_kills_running_and_finalises(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        from unittest.mock import MagicMock

        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        from src import jobs as jobs_mod
        run_dir = jobs_mod.new_run_dir(created["id"], "20260524T080000")
        jobs_mod.write_run_json(
            run_dir,
            run_id=run_dir.name,
            status="running",
            started_at="2026-05-24T08:00:00",
            pid=4242,
        )

        kill_spy = MagicMock(return_value=[4242, 4243])
        from app.webapp.routers import jobs as jobs_router
        monkeypatch.setattr(jobs_router, "_kill_process_tree", kill_spy)

        resp = client.post(
            "/api/jobs/" + created["id"] + "/runs/" + run_dir.name + "/kill"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["signalled"] == [4242, 4243]
        kill_spy.assert_called_once_with(4242)

        # The run record was finalised with the kill markers.
        record = jobs_mod.read_run(run_dir)
        assert record["status"] == "failed"
        assert record["exit_code"] == -9
        assert record["killed"] is True
        assert record.get("finished_at")
