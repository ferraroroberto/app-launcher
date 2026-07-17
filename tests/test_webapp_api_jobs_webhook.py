"""``POST /api/jobs/<id>/hook`` — webhook-target jobs (issue #73).

Schtasks + the detached executor spawn are mocked at the router-module
level (via the shared ``mocked_jobs_side_effects`` fixture) so no real
schtasks or subprocess runs. Signature verification uses real HMAC — no
mocking of ``src.jobs_webhook`` — so these tests exercise the actual
crypto, not a stub.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
from pathlib import Path

import pytest


def _stub_path(name: str = "hook.py") -> str:
    root = Path(tempfile.mkdtemp(prefix="al-hook-stub-"))
    venv_py = root / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("", encoding="utf-8")
    p = root / name
    p.write_text("print('ok')\n", encoding="utf-8")
    return str(p)


def _github_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _stripe_sig(secret: str, body: bytes, t: int) -> str:
    signed = f"{t}.".encode() + body
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


@pytest.fixture
def mocked_jobs_side_effects(monkeypatch):
    from unittest.mock import MagicMock
    from app.webapp.routers import jobs as jobs_router

    mocks = {
        "sync_schtasks": MagicMock(return_value=[]),
        "delete_schtasks": MagicMock(return_value=[]),
        "query_next_run": MagicMock(return_value=None),
        "spawn_run_job_detached": MagicMock(return_value=1234),
        "run_stats": MagicMock(
            return_value={
                "p50": None, "p95": None, "success_rate_30d": None,
                "completed_count": 0, "last7": [],
            }
        ),
        "is_stuck": MagicMock(return_value=False),
    }
    for name, m in mocks.items():
        monkeypatch.setattr(jobs_router.jobs_mod, name, m)
    return mocks


def _seed_github_job(client, *, secret="s3cr3t", events=None, mapping=None, params=None):
    payload = {
        "name": "GitHub Hook Job",
        "script_path": _stub_path("gh.py"),
        "params": params or [
            {"name": "repo", "kind": "string", "flag": "--repo"},
            {"name": "branch", "kind": "string", "flag": "--branch"},
        ],
        "webhook": {
            "provider": "github",
            "secret": secret,
            "mapping": mapping if mapping is not None else {
                "repo": "$.repository.full_name",
                "branch": "$.ref",
            },
            "events": events or [],
        },
    }
    resp = client.post("/api/jobs", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["job"]


def _seed_stripe_job(client, *, secret="whsec_test"):
    payload = {
        "name": "Stripe Hook Job",
        "script_path": _stub_path("stripe.py"),
        "params": [{"name": "intent_id", "kind": "string", "flag": "--intent-id"}],
        "webhook": {
            "provider": "stripe",
            "secret": secret,
            "mapping": {"intent_id": "$.data.object.id"},
        },
    }
    resp = client.post("/api/jobs", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["job"]


def _seed_generic_job(client, *, secret="tok123"):
    payload = {
        "name": "Generic Hook Job",
        "script_path": _stub_path("generic.py"),
        "webhook": {"provider": "generic", "secret": secret},
    }
    resp = client.post("/api/jobs", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["job"]


def _run_count(client, job_id):
    resp = client.get(f"/api/jobs/{job_id}/runs")
    assert resp.status_code == 200
    return len(resp.json()["runs"])


class TestCreateWithWebhook:
    def test_webhook_round_trips(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        job = _seed_github_job(client)
        assert job["webhook"]["provider"] == "github"
        assert job["webhook"]["mapping"]["repo"] == "$.repository.full_name"

    def test_bad_provider_rejected(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/jobs",
            json={
                "name": "Bad",
                "script_path": _stub_path("bad.py"),
                "webhook": {"provider": "bitbucket", "secret": "x"},
            },
        )
        assert resp.status_code == 400


class TestGithubHook:
    def test_valid_signature_fires_run_with_mapped_params(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = _seed_github_job(client)
        payload = {
            "repository": {"full_name": "acme/widgets"},
            "ref": "refs/heads/main",
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={
                "X-Hub-Signature-256": _github_sig("s3cr3t", body),
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]

        run = client.get(f"/api/jobs/{job['id']}/runs/{run_id}").json()["run"]
        assert run["trigger"] == "webhook"
        assert run["trigger_source"] == "webhook:github"
        assert run["params"] == {"repo": "acme/widgets", "branch": "refs/heads/main"}
        assert run["webhook_payload"]["provider"] == "github"
        assert run["webhook_payload"]["event"] == "push"
        assert run["webhook_payload"]["payload"] == payload
        # The signature header itself must never be persisted.
        assert "x-hub-signature-256" not in run["webhook_payload"]["headers"]

    def test_bad_signature_401_no_run_record(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = _seed_github_job(client)
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={"X-Hub-Signature-256": _github_sig("wrong-secret", body)},
        )
        assert resp.status_code == 401
        assert _run_count(client, job["id"]) == 0

    def test_missing_signature_401(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        job = _seed_github_job(client)
        resp = client.post(f"/api/jobs/{job['id']}/hook", content=b"{}")
        assert resp.status_code == 401
        assert _run_count(client, job["id"]) == 0

    def test_event_allowlist_miss_204_no_run(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = _seed_github_job(client, events=["push"])
        body = json.dumps({"action": "opened"}).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={
                "X-Hub-Signature-256": _github_sig("s3cr3t", body),
                "X-GitHub-Event": "issues",
            },
        )
        assert resp.status_code == 204
        assert _run_count(client, job["id"]) == 0

    def test_event_allowlist_hit_fires(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = _seed_github_job(client, events=["push"])
        body = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "a/b"}}).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={
                "X-Hub-Signature-256": _github_sig("s3cr3t", body),
                "X-GitHub-Event": "push",
            },
        )
        assert resp.status_code == 200
        assert _run_count(client, job["id"]) == 1

    def test_no_webhook_configured_404(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = client.post(
            "/api/jobs",
            json={"name": "Plain", "script_path": _stub_path("plain.py")},
        ).json()["job"]
        resp = client.post(f"/api/jobs/{job['id']}/hook", content=b"{}")
        assert resp.status_code == 404

    def test_unknown_job_404(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        resp = client.post("/api/jobs/does-not-exist/hook", content=b"{}")
        assert resp.status_code == 404

    def test_unmapped_param_400_no_run_record(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        # Mapping targets a param name the job never declared.
        job = _seed_github_job(
            client,
            mapping={"nope": "$.repository.full_name"},
            params=[],
        )
        body = json.dumps({"repository": {"full_name": "a/b"}}).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={"X-Hub-Signature-256": _github_sig("s3cr3t", body)},
        )
        assert resp.status_code == 400
        assert _run_count(client, job["id"]) == 0


class TestStripeHook:
    def test_valid_signature_maps_intent_id(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = _seed_stripe_job(client)
        payload = {"data": {"object": {"id": "pi_12345"}}}
        body = json.dumps(payload).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={
                "Stripe-Signature": _stripe_sig("whsec_test", body, t=int(time.time()))
            },
        )
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        run = client.get(f"/api/jobs/{job['id']}/runs/{run_id}").json()["run"]
        assert run["trigger_source"] == "webhook:stripe"
        assert run["params"] == {"intent_id": "pi_12345"}

    def test_wrong_secret_401(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        job = _seed_stripe_job(client)
        body = json.dumps({"data": {"object": {"id": "pi_1"}}}).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={
                "Stripe-Signature": _stripe_sig("wrong", body, t=int(time.time()))
            },
        )
        assert resp.status_code == 401
        assert _run_count(client, job["id"]) == 0


class TestGenericHook:
    def test_matching_token_fires(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        job = _seed_generic_job(client)
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=b"{}",
            headers={"X-Webhook-Token": "tok123"},
        )
        assert resp.status_code == 200
        assert _run_count(client, job["id"]) == 1

    def test_mismatched_token_401(self, webapp_client, mocked_jobs_side_effects):
        client, _, _ = webapp_client
        job = _seed_generic_job(client)
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=b"{}",
            headers={"X-Webhook-Token": "wrong"},
        )
        assert resp.status_code == 401
        assert _run_count(client, job["id"]) == 0


class TestSecretResolution:
    def test_secret_ref_resolves_against_webapp_config(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, app, _ = webapp_client
        app.state.webapp_config.secrets = {"gh_key": "resolved-secret"}
        job = _seed_github_job(client, secret="$secret:gh_key")
        body = json.dumps(
            {"repository": {"full_name": "a/b"}, "ref": "refs/heads/main"}
        ).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={"X-Hub-Signature-256": _github_sig("resolved-secret", body)},
        )
        assert resp.status_code == 200

    def test_unresolvable_secret_ref_401(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, _, _ = webapp_client
        job = _seed_github_job(client, secret="$secret:missing-key")
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={"X-Hub-Signature-256": _github_sig("anything", body)},
        )
        assert resp.status_code == 401
        assert _run_count(client, job["id"]) == 0


class TestBearerExemption:
    def test_hook_route_bypasses_bearer_even_when_auth_token_set(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, app, _ = webapp_client
        job = _seed_github_job(client)
        app.state.webapp_config.auth_token = "supersecret"
        body = json.dumps(
            {"repository": {"full_name": "a/b"}, "ref": "refs/heads/main"}
        ).encode()
        resp = client.post(
            f"/api/jobs/{job['id']}/hook",
            content=body,
            headers={"X-Hub-Signature-256": _github_sig("s3cr3t", body)},
            # deliberately no Authorization header / ?token=
        )
        assert resp.status_code == 200

    def test_run_route_still_requires_bearer_when_auth_token_set(
        self, webapp_client, mocked_jobs_side_effects
    ):
        client, app, _ = webapp_client
        job = _seed_github_job(client)
        app.state.webapp_config.auth_token = "supersecret"
        resp = client.post(f"/api/jobs/{job['id']}/run")
        assert resp.status_code == 401
