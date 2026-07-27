"""``POST /sessions/{sid}/input`` (app/session_host/server.py) — issue #607.

Before this fix the route unconditionally returned ``{"ok": true}`` even when
``PtySession.write`` silently dropped the payload (session already exited but
not yet reaped, or the underlying PTY write raised) — a caller (chief's
steering nudge) had no way to tell a delivered message from a lost one. The
route now surfaces a drop as HTTP 409 instead of a false 200.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server


def test_input_delivered_returns_ok(monkeypatch):
    session = MagicMock()
    session.write.return_value = True
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "hello"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    session.write.assert_called_once_with("hello")


def test_input_dropped_returns_409_not_false_ok(monkeypatch):
    """The exited-but-not-yet-reaped case (up to the 30s reap window): the
    session is still findable via manager.get() but write() reports the
    drop. Must not come back as {"ok": true}."""
    session = MagicMock()
    session.write.return_value = False
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "hello"})

    assert resp.status_code == 409
    assert resp.json() != {"ok": True}


def test_input_unknown_session_returns_404(monkeypatch):
    monkeypatch.setattr(server.manager, "get", lambda sid: None)
    client = TestClient(server.app)

    resp = client.post("/sessions/no-such/input", json={"data": "hello"})

    assert resp.status_code == 404
