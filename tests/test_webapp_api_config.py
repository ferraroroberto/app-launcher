"""GET / POST /api/config — shape + allow-list + validation."""

from __future__ import annotations

import pytest


class TestGetConfig:
    def test_returns_expected_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "host" in body
        assert "port" in body
        assert "projects_dir" in body
        assert "apps_scan_root" in body
        assert "claude" in body
        # auth_password_set is what the SPA shows in the login overlay
        # ("a password is required" vs not). Bool, not the password itself.
        assert isinstance(body["auth_password_set"], bool)
        assert body["auth_password_set"] is False  # default conftest config

    def test_claude_block_carries_all_known_keys(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        claude = body["claude"]
        for key in (
            "model",
            "effort",
            "verbose",
            "debug",
            "models_available",
            "efforts_available",
            "always_on_flags",
            "computed_flags",
        ):
            assert key in claude, f"missing key {key} in /api/config claude block"


class TestPatchConfig:
    def test_patches_allowed_field(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_effort": "low"}
        )
        assert resp.status_code == 200
        assert "--effort low" in resp.json()["claude_flags"]
        # And the in-memory cfg was swapped.
        assert app.state.webapp_config.claude_effort == "low"

    def test_rejects_invalid_value_with_400(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_model": "definitely-not-a-real-model"}
        )
        assert resp.status_code == 400

    def test_ignores_unknown_field_silently(self, webapp_client):
        """The endpoint filters by allow-list — unknown keys are dropped,
        not error'd. Confirms the whitelist isn't accidentally loosened."""
        client, app, _ = webapp_client
        before = app.state.webapp_config.claude_model
        resp = client.post(
            "/api/config",
            json={"auth_token": "should-be-ignored", "claude_model": before},
        )
        assert resp.status_code == 200
        # auth_token is NOT in the allow-list and must not be patched here.
        assert app.state.webapp_config.auth_token == ""
