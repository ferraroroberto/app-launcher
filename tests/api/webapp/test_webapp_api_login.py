"""POST /api/login — password→token exchange + auth gate behaviour.

The bearer middleware skips loopback callers (server.py:267) but the
FastAPI ``TestClient`` reports its client host as ``"testclient"``, NOT
``127.0.0.1`` — so the middleware runs in these tests. That's the point:
we want to validate the gate, not bypass it.
"""

from __future__ import annotations


class TestLogin:
    def test_503_when_no_password_configured(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post("/api/login", json={"password": "anything"})
        # Default config has no auth_password — endpoint refuses to mint
        # a token. 503 distinguishes "not configured" from "bad password".
        assert resp.status_code == 503

    def test_503_when_password_set_but_no_token(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_password = "hunter2"
        # auth_token deliberately left empty.
        resp = client.post("/api/login", json={"password": "hunter2"})
        assert resp.status_code == 503

    def test_401_on_wrong_password(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_password = "hunter2"
        app.state.webapp_config.auth_token = "secret-token"
        resp = client.post("/api/login", json={"password": "wrong"})
        assert resp.status_code == 401

    def test_200_with_token_on_correct_password(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_password = "hunter2"
        app.state.webapp_config.auth_token = "secret-token"
        resp = client.post("/api/login", json={"password": "hunter2"})
        assert resp.status_code == 200
        assert resp.json() == {"token": "secret-token"}


class TestBearerGate:
    """Sanity-check that turning on the token actually gates other endpoints."""

    def test_authed_endpoint_401_without_token(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        resp = client.get("/api/config")  # not in _AUTH_EXEMPT
        assert resp.status_code == 401

    def test_authed_endpoint_ok_with_bearer_header(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        resp = client.get(
            "/api/config",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status_code == 200

    def test_authed_endpoint_ok_with_token_query_param(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        resp = client.get("/api/config?token=secret-token")
        assert resp.status_code == 200

    def test_healthz_always_exempt(self, webapp_client):
        client, app, _ = webapp_client
        app.state.webapp_config.auth_token = "secret-token"
        resp = client.get("/healthz")
        assert resp.status_code == 200
