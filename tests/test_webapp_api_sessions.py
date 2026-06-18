"""/api/claude-code/sessions — list + stop (kill/quit/interrupt modes)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import InvalidHandshake


class TestListSessions:
    def test_empty_list_when_session_host_returns_none(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        resp = client.get("/api/claude-code/sessions")
        assert resp.status_code == 200
        assert resp.json() == {"sessions": []}

    def test_lists_sessions_from_session_host(self, webapp_client):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [
            {
                "session_id": "abc-123",
                "kind": "pty",
                "name": "MyProject",
                "project_dir": "C:\\stub",
            }
        ]
        resp = client.get("/api/claude-code/sessions")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "abc-123"

    def test_session_host_error_returns_empty_not_500(self, webapp_client):
        """When session-host is down, the SPA should still render — the
        list endpoint logs and returns empty rather than 500ing."""
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.list_sessions.side_effect = sess.SessionHostError(
            "session-host unreachable", status=503
        )
        resp = client.get("/api/claude-code/sessions")
        assert resp.status_code == 200
        assert resp.json() == {"sessions": []}


class TestStopSession:
    def test_kill_mode_forwarded_to_session_client(self, webapp_client):
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True, "mode": "kill"}
        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "kill"},
        )
        assert resp.status_code == 200
        # Exact arg shape — session-host port from default config + sid +
        # mode — mirrors session_client.stop signature (issue #253 dropped
        # the close_window axis; every stop now closes).
        sess.stop.assert_called_once_with(8446, "abc-123", "kill")

    def test_quit_mode_forwarded_to_session_client(self, webapp_client):
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True}
        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "quit"},
        )
        assert resp.status_code == 200
        sess.stop.assert_called_once_with(8446, "abc-123", "quit")

    def test_default_mode_is_quit(self, webapp_client):
        """Empty body → mode falls back to quit per the endpoint contract."""
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True}
        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop", json={}
        )
        assert resp.status_code == 200
        sess.stop.assert_called_once_with(8446, "abc-123", "quit")

    def test_session_host_error_maps_to_http_status(self, webapp_client):
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.side_effect = sess.SessionHostError(
            "no such session", status=404
        )
        resp = client.post(
            "/api/claude-code/sessions/missing/stop",
            json={"mode": "kill"},
        )
        assert resp.status_code == 404
        assert "no such session" in resp.json()["detail"]


class TestStopSessionMirrorClose:
    """Issue #20 / #253: every stop must also dismiss the PC mirror window.

    Since #253 unified the button, every stop closes the window, so the
    webapp's stop route always asks ``launcher.close_mirror_window`` to
    PostMessage WM_CLOSE to the stashed HWND, on top of the cooperative
    WS-shutdown frame the session-host fires. Either path is enough on its
    own — both run because the cooperative one is silent if the page is
    unresponsive, and the Win32 one is silent if the HWND was never
    captured (e.g. launch came from the PC itself).
    """

    def test_mirror_close_always_invoked_and_stop_forwarded(
        self, webapp_client, monkeypatch
    ):
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True, "mode": "quit"}
        # Stub the launcher hook the sessions router will call.
        from app.webapp.routers import sessions as sessions_router
        mock_close = MagicMock(return_value=True)
        monkeypatch.setattr(
            sessions_router.launcher, "close_mirror_window", mock_close
        )

        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "quit"},
        )

        assert resp.status_code == 200
        # Even a plain graceful quit closes the mirror window now (#253).
        mock_close.assert_called_once_with("abc-123")
        sess.stop.assert_called_once_with(8446, "abc-123", "quit")

    def test_mirror_close_no_stashed_hwnd_still_forwards(
        self, webapp_client, monkeypatch
    ):
        """When the HWND lookup never captured one (launch from PC, or
        the title-set race lost), the mirror-close is a no-op but the
        session-host stop still goes through — cooperative WS shutdown
        is the fallback path."""
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True}
        from app.webapp.routers import sessions as sessions_router
        # close_mirror_window returns False — HWND was never stashed.
        mock_close = MagicMock(return_value=False)
        monkeypatch.setattr(
            sessions_router.launcher, "close_mirror_window", mock_close
        )

        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "kill"},
        )

        assert resp.status_code == 200
        mock_close.assert_called_once_with("abc-123")
        sess.stop.assert_called_once_with(8446, "abc-123", "kill")

    def test_mirror_close_failure_does_not_break_stop(
        self, webapp_client, monkeypatch
    ):
        """If WM_CLOSE PostMessage blows up, the stop request must still
        succeed — the session-host kill is the load-bearing part."""
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True}
        from app.webapp.routers import sessions as sessions_router
        mock_close = MagicMock(side_effect=OSError("hwnd is rubbish"))
        monkeypatch.setattr(
            sessions_router.launcher, "close_mirror_window", mock_close
        )

        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "kill"},
        )

        assert resp.status_code == 200
        sess.stop.assert_called_once_with(8446, "abc-123", "kill")


class TestProxySessionWS:
    """Issue #61: an upstream WS handshake rejection must not escape.

    When the session-host rejects the upstream WS upgrade at the HTTP
    layer (e.g. 403 for a reaped/unknown session, raised by the
    ``websockets`` client as ``InvalidStatus`` — a subclass of
    ``InvalidHandshake``), the proxy must close the browser socket
    cleanly with code 4502 rather than raising an unhandled ASGI
    exception with a full traceback in the webapp log.
    """

    def _patch_loopback(self, sessions_router, monkeypatch):
        """TestClient connects as host 'testclient'; treat it as loopback
        so the Tailscale/passkey gate is skipped and the proxy reaches
        the upstream ``ws_connect`` call under test."""
        monkeypatch.setattr(
            sessions_router,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    def test_upstream_handshake_rejection_closes_4502(
        self, webapp_client, monkeypatch
    ):
        client, _, _ = webapp_client
        from app.webapp.routers import sessions as sessions_router
        self._patch_loopback(sessions_router, monkeypatch)

        class _RejectingConnect:
            """Stand-in for ``ws_connect`` whose handshake is rejected."""

            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                raise InvalidHandshake("simulated session-host 403")

            async def __aexit__(self, *exc) -> bool:
                return False

        monkeypatch.setattr(sessions_router, "ws_connect", _RejectingConnect)

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/api/claude-code/sessions/reaped-sid/ws"
            ) as ws:
                ws.receive_text()
        assert excinfo.value.code == 4502

    def test_upstream_unreachable_still_closes_4502(
        self, webapp_client, monkeypatch
    ):
        """Regression guard: the existing OSError path (session-host not
        listening at all) keeps mapping to the same 4502 close."""
        client, _, _ = webapp_client
        from app.webapp.routers import sessions as sessions_router
        self._patch_loopback(sessions_router, monkeypatch)

        class _UnreachableConnect:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                raise OSError("connection refused")

            async def __aexit__(self, *exc) -> bool:
                return False

        monkeypatch.setattr(sessions_router, "ws_connect", _UnreachableConnect)

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/api/claude-code/sessions/no-host-sid/ws"
            ) as ws:
                ws.receive_text()
        assert excinfo.value.code == 4502
