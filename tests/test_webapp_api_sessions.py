"""/api/claude-code/sessions — list + stop (kill/quit/interrupt modes)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


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
            json={"mode": "kill", "close_window": True},
        )
        assert resp.status_code == 200
        # Exact arg shape — session-host port from default config + sid +
        # mode + close_window flag — mirrors session_client.stop signature.
        sess.stop.assert_called_once_with(8446, "abc-123", "kill", True)

    def test_quit_mode_default_close_window_false(self, webapp_client):
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True}
        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "quit"},
        )
        assert resp.status_code == 200
        # close_window defaults to False when omitted from the body.
        sess.stop.assert_called_once_with(8446, "abc-123", "quit", False)

    def test_default_mode_is_quit(self, webapp_client):
        """Empty body → mode falls back to quit per the endpoint contract."""
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True}
        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop", json={}
        )
        assert resp.status_code == 200
        sess.stop.assert_called_once_with(8446, "abc-123", "quit", False)

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
    """Issue #20: Stop & Close must also dismiss the PC mirror window.

    The cooperative WS-shutdown fallback (step 7) still goes through
    via ``session_client.stop(..., close_window=True)``; on top of
    that, the webapp's stop route asks ``launcher.close_mirror_window``
    to PostMessage WM_CLOSE to the stashed HWND. Either path is enough
    on its own — both run because the cooperative one is silent if the
    page is unresponsive, and the Win32 one is silent if the HWND was
    never captured (e.g. launch came from the PC itself).
    """

    def test_close_window_true_invokes_mirror_close_and_forwards(
        self, webapp_client, monkeypatch
    ):
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True, "mode": "kill", "close_window": True}
        # Stub the launcher hook the sessions router will call.
        from app.webapp.routers import sessions as sessions_router
        from src import launcher as real_launcher
        mock_close = MagicMock(return_value=True)
        monkeypatch.setattr(
            sessions_router.launcher, "close_mirror_window", mock_close
        )

        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "kill", "close_window": True},
        )

        assert resp.status_code == 200
        mock_close.assert_called_once_with("abc-123")
        # Cooperative WS shutdown still goes through to the session-host.
        sess.stop.assert_called_once_with(8446, "abc-123", "kill", True)

    def test_close_window_false_does_not_call_mirror_close(
        self, webapp_client, monkeypatch
    ):
        """Plain Stop (not Stop & Close) leaves the PC window open."""
        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.stop.return_value = {"ok": True}
        from app.webapp.routers import sessions as sessions_router
        mock_close = MagicMock(return_value=True)
        monkeypatch.setattr(
            sessions_router.launcher, "close_mirror_window", mock_close
        )

        resp = client.post(
            "/api/claude-code/sessions/abc-123/stop",
            json={"mode": "quit", "close_window": False},
        )

        assert resp.status_code == 200
        mock_close.assert_not_called()

    def test_close_window_true_no_stashed_hwnd_still_forwards(
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
            json={"mode": "kill", "close_window": True},
        )

        assert resp.status_code == 200
        mock_close.assert_called_once_with("abc-123")
        sess.stop.assert_called_once_with(8446, "abc-123", "kill", True)

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
            json={"mode": "kill", "close_window": True},
        )

        assert resp.status_code == 200
        sess.stop.assert_called_once_with(8446, "abc-123", "kill", True)
