"""/api/jobs save-time pre-flight, dry run, confirm-on-fire and alert-on-failure."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


from tests.api.jobs._support import _seed_one_job, _stub_path, _stub_path_no_venv


# ============================================================ pre-flight (#69)


class TestPreflightOnSave:
    """Save-time pre-flight gate on POST / PUT (issue #69 PR #1)."""

    def test_post_nonexistent_script_blocks_with_problems(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={"name": "Ghost", "script_path": "C:\\does\\not\\exist.py"},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["reason"] == "preflight"
        problems = detail["problems"]
        assert any(
            p["level"] == "error" and p["field"] == "script_path"
            for p in problems
        )
        # Nothing was persisted — the row never reached the registry.
        assert client.get("/api/jobs").json()["jobs"] == []

    def test_post_py_without_venv_warns_and_holds_save(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={"name": "NoVenv", "script_path": _stub_path_no_venv("nv.py")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Warnings-only, not acknowledged → not saved yet.
        assert body["saved"] is False
        assert any(
            p["level"] == "warning" and p["field"] == "script_path"
            for p in body["warnings"]
        )
        assert client.get("/api/jobs").json()["jobs"] == []

    def test_post_with_acknowledge_saves_with_warnings(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "NoVenv",
                "script_path": _stub_path_no_venv("nv2.py"),
                "acknowledge_warnings": True,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["saved"] is True
        assert body["job"]["name"] == "NoVenv"
        assert body["warnings"]  # the warning is echoed back
        assert len(client.get("/api/jobs").json()["jobs"]) == 1

    def test_post_unbalanced_args_quote_saves(
        self, webapp_client, mocked_jobs_side_effects
    ):
        # The executor uses plain str.split() (whitespace), so an unbalanced
        # quote is not a parse error — the save must succeed (200), not 400.
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "UnbalancedArgs",
                "script_path": _stub_path("demo.bat"),
                "args": 'foo "unbalanced',
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["saved"] is True
        assert not any(p["field"] == "args" for p in body.get("problems", []))

    def test_clean_save_reports_no_warnings(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = _seed_one_job(client, name="Clean")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved"] is True
        assert body["warnings"] == []

    def test_put_to_nonexistent_script_blocks(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={"script_path": "C:\\nope\\gone.py"},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["reason"] == "preflight"


# ============================================================ dry-run (#69)


class TestDryRun:
    """Dry-run modes on POST /api/jobs/<id>/run (issue #69 PR #2)."""

    def test_check_writes_synthetic_record_without_spawning(
        self, webapp_client, mocked_jobs_side_effects, monkeypatch
    ):
        client, _, overrides = webapp_client
        # A real script so build_invocation resolves cleanly.
        created = _seed_one_job(
            client, name="Demo", script=_stub_path("dry_ok.py")
        ).json()["job"]
        resp = client.post(
            "/api/jobs/" + created["id"] + "/run",
            json={"dry_run": "check"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dry_run"] is True
        assert body["status"] == "dry_run_success"
        # No child was spawned for a check.
        assert not mocked_jobs_side_effects["spawn_run_job_detached"].called
        # The record is stamped dry_run + carries the synthetic status.
        run_dir = overrides["tmp_jobs_runs_dir"] / created["id"] / body["run_id"]
        record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert record["status"] == "dry_run_success"
        assert record["dry_run"] is True
        assert "exit_code" not in record  # null → not written

    def test_check_on_missing_script_reports_failed(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, overrides = webapp_client
        # Seed with a real script, then delete it so build_invocation fails.
        path = _stub_path("vanishing.py")
        created = _seed_one_job(client, name="Demo", script=path).json()["job"]
        Path(path).unlink()
        resp = client.post(
            "/api/jobs/" + created["id"] + "/run",
            json={"dry_run": "check"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "dry_run_failed"

    def test_execute_spawns_with_dry_flag(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(
            client, name="Demo", script=_stub_path("dry_exec.py")
        ).json()["job"]
        resp = client.post(
            "/api/jobs/" + created["id"] + "/run",
            json={"dry_run": "execute"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dry_run"] is True
        spawn = mocked_jobs_side_effects["spawn_run_job_detached"]
        assert spawn.called
        # 5th positional arg is the dry_run flag.
        assert spawn.call_args.args[4] is True

    def test_invalid_dry_run_value_rejected(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post(
            "/api/jobs/" + created["id"] + "/run",
            json={"dry_run": "bogus"},
        )
        assert resp.status_code == 400

    def test_dry_check_bypasses_cooldown(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, overrides = webapp_client
        created = client.post(
            "/api/jobs",
            json={
                "name": "Cool",
                "script_path": _stub_path("x.py"),
                "cooldown_seconds": 30,
            },
        ).json()["job"]
        # Anchor a real run 5 s ago — a normal fire would 429.
        now = datetime.now().replace(microsecond=0)
        run_dir = overrides["tmp_jobs_runs_dir"] / created["id"] / "20260101T000000"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps({
                "run_id": "20260101T000000",
                "job_id": created["id"],
                "status": "success",
                "started_at": (now - timedelta(seconds=5)).isoformat(
                    timespec="seconds"
                ),
            }),
            encoding="utf-8",
        )
        # A normal fire is cooled down...
        assert client.post(f"/api/jobs/{created['id']}/run").status_code == 429
        # ...but a dry-run check sails through.
        resp = client.post(
            f"/api/jobs/{created['id']}/run", json={"dry_run": "check"}
        )
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True


# ========================================================= confirm-on-fire (#69)


class TestConfirmOnFire:
    """``confirm`` flag gates manual fires (issue #69 PR #3)."""

    def _seed_confirm_job(self, client):
        return client.post(
            "/api/jobs",
            json={
                "name": "Destructive",
                "script_path": _stub_path("danger.bat"),
                "confirm": True,
            },
        ).json()["job"]

    def test_create_round_trips_confirm(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_confirm_job(client)
        assert job["confirm"] is True

    def test_confirm_round_trips_through_put(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        r = client.put("/api/jobs/" + created["id"], json={"confirm": True})
        assert r.status_code == 200
        assert r.json()["job"]["confirm"] is True
        # Clearing it drops the key (omitted when False).
        r = client.put("/api/jobs/" + created["id"], json={"confirm": False})
        assert r.status_code == 200
        assert "confirm" not in r.json()["job"]

    def test_unconfirmed_fire_is_403(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_confirm_job(client)
        resp = client.post("/api/jobs/" + job["id"] + "/run")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "confirmation required"
        assert not mocked_jobs_side_effects["spawn_run_job_detached"].called

    def test_confirmed_fire_runs(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_confirm_job(client)
        resp = client.post("/api/jobs/" + job["id"] + "/run?confirmed=1")
        assert resp.status_code == 200, resp.text
        assert mocked_jobs_side_effects["spawn_run_job_detached"].called

    def test_dry_check_is_exempt_from_confirm(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_confirm_job(client)
        # A check has no side effects → no confirmation needed.
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run", json={"dry_run": "check"}
        )
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True

    def test_dry_execute_still_needs_confirm(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = self._seed_confirm_job(client)
        # 'execute' actually spawns → gated like a normal fire.
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run", json={"dry_run": "execute"}
        )
        assert resp.status_code == 403
        resp = client.post(
            "/api/jobs/" + job["id"] + "/run?confirmed=1",
            json={"dry_run": "execute"},
        )
        assert resp.status_code == 200

    def test_unflagged_job_needs_no_confirmation(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.post("/api/jobs/" + created["id"] + "/run")
        assert resp.status_code == 200


# ================================================ alert-on-failure (#597)


class TestAlertOnFailure:
    """``alert_on_failure`` flag round-trips through create/edit (issue #597)."""

    def test_create_round_trips_alert_on_failure(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = client.post(
            "/api/jobs",
            json={
                "name": "Reporting Pipeline",
                "script_path": _stub_path("report.py"),
                "alert_on_failure": True,
            },
        ).json()["job"]
        assert job["alert_on_failure"] is True

    def test_alert_on_failure_round_trips_through_put(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        r = client.put("/api/jobs/" + created["id"], json={"alert_on_failure": True})
        assert r.status_code == 200
        assert r.json()["job"]["alert_on_failure"] is True
        # Clearing it drops the key (omitted when False).
        r = client.put("/api/jobs/" + created["id"], json={"alert_on_failure": False})
        assert r.status_code == 200
        assert "alert_on_failure" not in r.json()["job"]

    def test_default_omitted_from_response(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        assert "alert_on_failure" not in created
