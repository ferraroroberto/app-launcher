"""/api/jobs create, list, edit, delete and the job-kind registry."""

from __future__ import annotations



from tests.api.jobs._support import _seed_one_job, _stub_path


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
                "script_path": _stub_path("demo.bat"),
            },
        )
        assert second.status_code == 409


class TestListJobs:
    def test_empty_initially(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["jobs"] == []
        # The runtime snapshot's age rides every answer (#1324), so a stale
        # one can never pass for current.
        assert set(body) == {"jobs", "snapshot"}
        assert set(body["snapshot"]) == {"built_at", "age_seconds"}

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


# ========================================================== job-kind (#70)


class TestJobKindRegistry:
    def test_create_inline_shell(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Inline demo",
                "kind": "inline-shell",
                "kind_config": {"script_body": "echo hi", "ext": ".bat"},
                "schedule": {"type": "none"},
            },
        )
        assert resp.status_code == 200, resp.text
        job = resp.json()["job"]
        assert job["kind"] == "inline-shell"
        assert job["kind_config"] == {"script_body": "echo hi", "ext": ".bat"}
        assert job["script_path"] == ""
        assert job["target_kind"] == "inline-shell"

    def test_create_http_check(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Health check",
                "kind": "http-check",
                "kind_config": {"url": "https://example.com/health"},
                "schedule": {"type": "hourly", "every": 1},
            },
        )
        assert resp.status_code == 200, resp.text
        job = resp.json()["job"]
        assert job["kind"] == "http-check"
        assert job["target_kind"] == "http-check"

    def test_create_http_check_missing_url_is_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Health check",
                "kind": "http-check",
                "kind_config": {},
                "schedule": {"type": "none"},
            },
        )
        assert resp.status_code == 400

    def test_create_unknown_kind_is_400(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Bogus",
                "kind": "bogus-kind",
                "script_path": _stub_path("demo.bat"),
                "schedule": {"type": "none"},
            },
        )
        assert resp.status_code == 400

    def test_switch_file_kind_to_inline_shell(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={
                "kind": "inline-shell",
                "script_path": "",
                "kind_config": {"script_body": "echo hi", "ext": ".bat"},
            },
        )
        assert resp.status_code == 200, resp.text
        job = resp.json()["job"]
        assert job["kind"] == "inline-shell"
        assert job["script_path"] == ""

    def test_switch_to_inline_shell_without_clearing_script_path_is_400(
        self, webapp_client, mocked_jobs_side_effects
    ):
        # Switching kind without explicitly clearing script_path in the
        # same PUT body is rejected — the dialog always sends the full
        # payload (including an explicit clear), so this only happens on
        # a malformed/partial PUT.
        client, _, _ = webapp_client
        created = _seed_one_job(client, name="Demo").json()["job"]
        resp = client.put(
            "/api/jobs/" + created["id"],
            json={
                "kind": "inline-shell",
                "kind_config": {"script_body": "echo hi", "ext": ".bat"},
            },
        )
        assert resp.status_code == 400
