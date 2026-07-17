"""Scoped API tokens over HTTP — mint / list / revoke, middleware scope
enforcement, run-record provenance, and the env-secret no-leak contract
(issue #72).

The TestClient's client host is ``testclient`` (non-loopback), so the
bearer gate is live whenever a credential is configured — exactly the
tunnel/tailnet situation the feature targets.
"""

from __future__ import annotations

import json

import pytest

from src import jobs as jobs_mod


@pytest.fixture
def jobs_mocks(monkeypatch):
    """Stub schtasks + the detached executor spawn on src.jobs itself, so
    both the CRUD routes (jobs.py) and the shared run tail (jobs_run.py)
    see them."""
    from unittest.mock import MagicMock

    mocks = {
        "sync_schtasks": MagicMock(return_value=[]),
        "delete_schtasks": MagicMock(return_value=[]),
        "query_next_run": MagicMock(return_value=None),
        "spawn_run_job_detached": MagicMock(return_value=1234),
    }
    for name, m in mocks.items():
        monkeypatch.setattr(jobs_mod, name, m)
    return mocks


def _enable_legacy_auth(tmp_path, app, token="legacy-tok"):
    """Set auth_token on disk AND in app.state — mint/revoke persist via
    update_webapp_config (a disk read-modify-write), so an in-memory-only
    token would be silently dropped on the next save."""
    cfg_path = tmp_path / "webapp_config.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["auth_token"] = token
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    app.state.webapp_config.auth_token = token
    return token


def _auth(token):
    return {"Authorization": "Bearer " + token}


def _seed_job(client, tmp_path, headers, name="Demo", job_id=None, env=None):
    script = tmp_path / "demo.bat"
    if not script.exists():
        script.write_text("@echo off\n", encoding="utf-8")
    payload = {
        "name": name,
        "script_path": str(script),
        "schedule": {"type": "none"},
    }
    if job_id:
        payload["id"] = job_id
    if env:
        payload["env"] = env
    resp = client.post("/api/jobs", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["job"]["id"]


def _mint(client, headers, job_id, label="Deck"):
    resp = client.post(
        "/api/tokens", json={"label": label, "jobs": [job_id]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTokenLifecycle:
    def test_mint_returns_raw_once_and_hides_secret_material(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        job_id = _seed_job(client, tmp_path, _auth(legacy))
        body = _mint(client, _auth(legacy), job_id)
        assert body["token"]
        assert body["label"] == "Deck"
        assert body["scope"] == {"jobs": [job_id]}
        assert "hash" not in body and "salt" not in body
        # The list endpoint never exposes the raw token or its hash.
        listed = client.get("/api/tokens", headers=_auth(legacy))
        assert listed.status_code == 200
        assert body["token"] not in listed.text
        assert "hash" not in listed.text and "salt" not in listed.text
        assert listed.json()["tokens"][0]["id"] == body["id"]

    def test_mint_requires_label_and_known_job(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        job_id = _seed_job(client, tmp_path, _auth(legacy))
        assert (
            client.post(
                "/api/tokens", json={"label": "", "jobs": [job_id]},
                headers=_auth(legacy),
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/tokens", json={"label": "x", "jobs": ["ghost"]},
                headers=_auth(legacy),
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/tokens", json={"label": "x"}, headers=_auth(legacy)
            ).status_code
            == 400
        )

    def test_revoke_removes_and_invalidates(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        job_id = _seed_job(client, tmp_path, _auth(legacy))
        body = _mint(client, _auth(legacy), job_id)
        raw = body["token"]
        # Works before revocation...
        ok = client.post(f"/api/jobs/{job_id}/run", headers=_auth(raw))
        assert ok.status_code == 200
        resp = client.delete("/api/tokens/" + body["id"], headers=_auth(legacy))
        assert resp.status_code == 200
        assert client.get("/api/tokens", headers=_auth(legacy)).json()["tokens"] == []
        # ...and is a plain bad bearer afterwards.
        gone = client.post(f"/api/jobs/{job_id}/run", headers=_auth(raw))
        assert gone.status_code == 401
        assert (
            client.delete("/api/tokens/tok-ghost", headers=_auth(legacy)).status_code
            == 404
        )


class TestScopeEnforcement:
    def test_scoped_token_fires_only_its_job(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        allowed = _seed_job(client, tmp_path, _auth(legacy), name="Allowed")
        other = _seed_job(
            client, tmp_path, _auth(legacy), name="Other", job_id="other-job"
        )
        raw = _mint(client, _auth(legacy), allowed)["token"]

        ok = client.post(f"/api/jobs/{allowed}/run", headers=_auth(raw))
        assert ok.status_code == 200
        assert jobs_mocks["spawn_run_job_detached"].called

        denied = client.post(f"/api/jobs/{other}/run", headers=_auth(raw))
        assert denied.status_code == 403
        assert other in denied.json()["detail"]

    def test_scoped_token_rejected_off_the_run_path(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        job_id = _seed_job(client, tmp_path, _auth(legacy))
        raw = _mint(client, _auth(legacy), job_id)["token"]
        for method, path in (
            ("GET", "/api/jobs"),
            ("GET", "/api/tokens"),
            ("GET", f"/api/jobs/{job_id}/runs"),
            ("POST", "/api/tokens"),
        ):
            resp = client.request(method, path, headers=_auth(raw))
            assert resp.status_code == 403, (method, path, resp.status_code)
            assert "job-scoped" in resp.json()["detail"]

    def test_legacy_token_unchanged_and_garbage_rejected(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        job_id = _seed_job(client, tmp_path, _auth(legacy))
        _mint(client, _auth(legacy), job_id)
        # Legacy token keeps full access everywhere.
        assert client.get("/api/jobs", headers=_auth(legacy)).status_code == 200
        assert client.get("/api/tokens", headers=_auth(legacy)).status_code == 200
        assert (
            client.post(
                f"/api/jobs/{job_id}/run", headers=_auth(legacy)
            ).status_code
            == 200
        )
        # A wrong bearer is a 401, and no-credential is a 401.
        assert client.get("/api/jobs", headers=_auth("garbage")).status_code == 401
        assert client.get("/api/jobs").status_code == 401

    def test_minted_tokens_alone_enforce_the_gate(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        """auth_token empty + minted tokens present must NOT be an open gate."""
        client, app, _ = webapp_client
        job_id = _seed_job(client, tmp_path, {})  # gate off pre-mint
        body = _mint(client, {}, job_id)
        assert client.get("/api/jobs").status_code == 401
        # The scoped token still fires its job.
        assert (
            client.post(
                f"/api/jobs/{job_id}/run", headers=_auth(body["token"])
            ).status_code
            == 200
        )


class TestRunProvenance:
    def test_api_run_records_source_ip_ua(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        job_id = _seed_job(client, tmp_path, _auth(legacy))
        resp = client.post(
            f"/api/jobs/{job_id}/run",
            headers={**_auth(legacy), "User-Agent": "probe-agent/1.0"},
        )
        assert resp.status_code == 200
        latest = jobs_mod.latest_run(job_id)
        assert latest is not None
        assert latest.get("trigger") == "manual"
        assert latest.get("trigger_source") == "api"
        assert latest.get("trigger_ip") == "testclient"
        assert latest.get("trigger_ua") == "probe-agent/1.0"
        # Legacy token has no id — provenance must not invent one.
        assert "trigger_token_id" not in latest

    def test_scoped_token_run_records_token_identity(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        legacy = _enable_legacy_auth(tmp_path, app)
        job_id = _seed_job(client, tmp_path, _auth(legacy))
        body = _mint(client, _auth(legacy), job_id, label="Deck btn 3")
        resp = client.post(f"/api/jobs/{job_id}/run", headers=_auth(body["token"]))
        assert resp.status_code == 200
        latest = jobs_mod.latest_run(job_id)
        assert latest is not None
        assert latest.get("trigger_source") == "api"
        assert latest.get("trigger_token_id") == body["id"]
        assert latest.get("trigger_token_label") == "Deck btn 3"
        # The raw token itself never lands in the run record.
        assert body["token"] not in json.dumps(latest)


class TestEnvSecretNoLeak:
    def test_jobs_api_returns_refs_never_values(
        self, webapp_client, jobs_mocks, tmp_path
    ):
        client, app, _ = webapp_client
        app.state.webapp_config.secrets = {"linkedin": "super-secret-value"}
        job_id = _seed_job(
            client, tmp_path, {}, env={"API_KEY": "$secret:linkedin"}
        )
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert "$secret:linkedin" in resp.text
        assert "super-secret-value" not in resp.text
        detail = client.get(f"/api/jobs/{job_id}/runs")
        assert "super-secret-value" not in detail.text
