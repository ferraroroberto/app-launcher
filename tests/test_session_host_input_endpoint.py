"""``POST /sessions/{sid}/input`` (app/session_host/server.py) — issues #607/#611.

#607: before that fix, the route unconditionally returned ``{"ok": true}``
even when the write silently dropped (session already exited but not yet
reaped, or the underlying PTY write raised) — a caller (chief's steering
nudge) had no way to tell a delivered message from a lost one. The route now
surfaces a drop as HTTP 409 instead of a false 200.

#611: the route now delegates to ``PtySession.submit_input`` (data + submit
in one call) instead of the old bare ``write(data)`` — framing and the
settle-then-submit sequence are the session-host's own job now, ported from
the compose bar's ``framePaste``/``sendSubmit``/``bulkSettle``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server


def test_input_delivered_returns_ok(monkeypatch):
    session = MagicMock()
    session.submit_input.return_value = True
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "hello", "submit": True})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    session.submit_input.assert_called_once_with("hello", True)


def test_submit_false_forwarded(monkeypatch):
    session = MagicMock()
    session.submit_input.return_value = True
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    client.post("/sessions/sid/input", json={"data": "draft", "submit": False})

    session.submit_input.assert_called_once_with("draft", False)


def test_submit_defaults_true_when_omitted(monkeypatch):
    session = MagicMock()
    session.submit_input.return_value = True
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    client.post("/sessions/sid/input", json={"data": "hello"})

    session.submit_input.assert_called_once_with("hello", True)


def test_bare_submit_with_no_data(monkeypatch):
    """{"data": "", "submit": true} (#611 escape hatch) — release a stranded
    composer with no text write."""
    session = MagicMock()
    session.submit_input.return_value = True
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "", "submit": True})

    assert resp.status_code == 200
    session.submit_input.assert_called_once_with("", True)


def test_input_dropped_returns_409_not_false_ok(monkeypatch):
    """The exited-but-not-yet-reaped case (up to the 30s reap window): the
    session is still findable via manager.get() but submit_input() reports
    the drop. Must not come back as {"ok": true}."""
    session = MagicMock()
    session.submit_input.return_value = False
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
