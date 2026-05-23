"""/api/jobs surface — list, CRUD, run, history (issue #47).

Schtasks and the detached executor spawn are mocked at the router-module
level so no real `schtasks.exe` runs and no subprocess is left behind.
"""

from __future__ import annotations

from datetime import datetime

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
