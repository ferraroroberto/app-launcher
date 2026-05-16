"""/api/claude-code/sessions — list + stop (kill/quit/interrupt modes)."""

from __future__ import annotations

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
