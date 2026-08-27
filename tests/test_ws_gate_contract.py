"""Contract: the terminal gate reaches the same verdict on every protocol.

Two surfaces re-apply the same gate against the same config:

* ``app.webapp.middleware.BearerTokenMiddleware`` — the HTTP choke point.
* ``app.webapp.routers.sessions.proxy_session_ws`` — re-applied inline,
  because Starlette middleware never sees a WebSocket handshake.

Any divergence between them is invisible from the HTTP side, so these tests
pin the two invariants the HTTP half already states in its own comments:

1. A credential is required whenever *either* credential class is configured
   (``auth_token`` or minted ``api_tokens``) — "a config with only minted
   tokens must not be an open gate" (``middleware.py``).
2. A request that arrived over the public Cloudflare edge is never treated as
   the PC itself, whatever client address it presents.
"""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from src import api_tokens


@pytest.fixture
def minted_only_config(webapp_client):
    """Config with no legacy ``auth_token`` — only a minted full-scope one."""
    client, app, overrides = webapp_client
    record, raw = api_tokens.mint_token("stream-deck", "*")
    cfg = app.state.webapp_config
    cfg.auth_token = ""
    cfg.api_tokens = [record]
    return client, app, overrides, raw


def _force_remote_tailnet(monkeypatch):
    """Make the TestClient look like a tailnet device, not the PC."""
    from app.webapp.routers import sessions as sessions_router

    monkeypatch.setattr(
        sessions_router, "LOOPBACK_HOSTS", frozenset({"127.0.0.1", "::1"})
    )
    monkeypatch.setattr(
        sessions_router, "client_in_tailnet", lambda host, allow: True
    )


class _AllowingConnect:
    """Upstream stub that would succeed — so a pass can only mean the gate
    let the handshake through, never that the session-host was unreachable."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        raise AssertionError("gate allowed an uncredentialed terminal socket")

    async def __aexit__(self, *exc) -> bool:
        return False


def test_ws_requires_a_credential_when_only_minted_tokens_exist(
    minted_only_config, monkeypatch
):
    """With ``auth_token`` empty but minted tokens configured, an
    uncredentialed socket must be refused — the HTTP half already refuses it."""
    client, _, _, _ = minted_only_config
    from app.webapp.routers import sessions as sessions_router

    _force_remote_tailnet(monkeypatch)
    monkeypatch.setattr(sessions_router, "ws_connect", _AllowingConnect)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/claude-code/sessions/some-sid/ws"
        ) as ws:
            ws.receive_text()
    assert excinfo.value.code == 4401


def test_ws_accepts_a_valid_minted_token(minted_only_config, monkeypatch):
    """A minted full-scope token works on every HTTP route; it must not be
    rejected here (the gate reaches the upstream connect, then our stub)."""
    client, _, _, raw = minted_only_config
    from app.webapp.routers import sessions as sessions_router

    _force_remote_tailnet(monkeypatch)

    reached = {}

    class _RecordingConnect:
        def __init__(self, *args, **kwargs) -> None:
            reached["yes"] = True

        async def __aenter__(self):
            raise OSError("stop here — the gate already passed")

        async def __aexit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(sessions_router, "ws_connect", _RecordingConnect)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/claude-code/sessions/some-sid/ws?token={raw}"
        ) as ws:
            ws.receive_text()
    assert reached.get("yes"), "a valid minted token must pass the gate"


def test_ws_still_refuses_a_wrong_legacy_token(webapp_client, monkeypatch):
    """Regression guard: the legacy ``auth_token`` path keeps refusing."""
    client, app, _ = webapp_client
    from app.webapp.routers import sessions as sessions_router

    app.state.webapp_config.auth_token = "the-real-token"
    app.state.webapp_config.api_tokens = []
    _force_remote_tailnet(monkeypatch)
    monkeypatch.setattr(sessions_router, "ws_connect", _AllowingConnect)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/claude-code/sessions/some-sid/ws?token=wrong"
        ) as ws:
            ws.receive_text()
    assert excinfo.value.code == 4401


def test_edge_request_is_not_treated_as_the_pc(webapp_client, monkeypatch):
    """A request carrying the public edge's own header must not inherit the
    PC's bypass, whatever client address reaches the app."""
    client, app, _ = webapp_client
    from app.webapp import middleware as mw

    app.state.webapp_config.auth_token = "the-real-token"
    monkeypatch.setattr(
        mw, "LOOPBACK_HOSTS", frozenset({"testclient", "127.0.0.1", "::1"})
    )

    # Same request, same (loopback-shaped) client — only the edge header differs.
    assert client.get("/api/config").status_code == 200
    gated = client.get("/api/config", headers={"cf-ray": "abc123-AMS"})
    assert gated.status_code == 401
