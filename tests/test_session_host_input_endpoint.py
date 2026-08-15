"""``POST /sessions/{sid}/input`` (app/session_host/server.py) — #607/#611/#760.

#607: before that fix, the route unconditionally returned ``{"ok": true}``
even when the write silently dropped (session already exited but not yet
reaped, or the underlying PTY write raised) — a caller (chief's steering
nudge) had no way to tell a delivered message from a lost one. The route now
surfaces a drop as HTTP 409 instead of a false 200.

#611: the route now delegates to ``PtySession.submit_input`` (data + submit
in one call) instead of the old bare ``write(data)`` — framing and the
settle-then-submit sequence are the session-host's own job now, ported from
the compose bar's ``framePaste``/``sendSubmit``/``bulkSettle``.

#760: ``submit_input`` returns a full outcome rather than a bool, and the
route maps its distinct failure conditions to distinct responses — a payload
the terminal never echoed is a 502 (alive session, undelivered message), and
a 200 carries the verdict so an unconfirmed submit can't read as success.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server
from src.session_host import (
    INPUT_DROPPED,
    INPUT_NOT_INGESTED,
    INPUT_OK,
    INPUT_SETTLE_CAP,
    INPUT_UNVERIFIED,
    InputOutcome,
)


def _session(outcome: InputOutcome) -> MagicMock:
    session = MagicMock()
    session.submit_input.return_value = outcome
    return session


def test_input_delivered_returns_ok(monkeypatch):
    session = _session(
        InputOutcome(
            reason=INPUT_OK, ingested=True, submitted=True, submit_confirmed=True
        )
    )
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "hello", "submit": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["delivered"] is True
    assert body["reason"] == INPUT_OK
    assert body["submit_confirmed"] is True
    session.submit_input.assert_called_once_with("hello", True)


def test_submit_false_forwarded(monkeypatch):
    session = _session(InputOutcome(reason=INPUT_UNVERIFIED))
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    client.post("/sessions/sid/input", json={"data": "draft", "submit": False})

    session.submit_input.assert_called_once_with("draft", False)


def test_submit_defaults_true_when_omitted(monkeypatch):
    session = _session(InputOutcome(reason=INPUT_UNVERIFIED, submitted=True))
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    client.post("/sessions/sid/input", json={"data": "hello"})

    session.submit_input.assert_called_once_with("hello", True)


def test_bare_submit_with_no_data(monkeypatch):
    """{"data": "", "submit": true} (#611 escape hatch) — release a stranded
    composer with no text write."""
    session = _session(InputOutcome(reason=INPUT_UNVERIFIED, submitted=True))
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "", "submit": True})

    assert resp.status_code == 200
    session.submit_input.assert_called_once_with("", True)


def test_input_dropped_returns_409_not_false_ok(monkeypatch):
    """The exited-but-not-yet-reaped case (up to the 30s reap window): the
    session is still findable via manager.get() but submit_input() reports
    the drop. Must not come back as {"ok": true}."""
    session = _session(InputOutcome(reason=INPUT_DROPPED))
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "hello"})

    assert resp.status_code == 409
    assert resp.json() != {"ok": True}


def test_never_echoed_input_returns_502_not_a_false_ok(monkeypatch):
    """#760: the session is alive and still takes keyboard input, but the
    payload was never painted back — an undelivered message, reported as
    such at the API layer instead of as a success."""
    session = _session(
        InputOutcome(reason=INPUT_NOT_INGESTED, ingested=False, waited_ms=5000)
    )
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "steer"})

    assert resp.status_code == 502
    assert "never echoed" in resp.json()["detail"]


def test_unconfirmed_submit_is_reported_on_the_200(monkeypatch):
    """A payload that reached the terminal but whose submit fired blind at
    the settle cap still returns 200 — but must never look like a confirmed
    delivery, or the caller is back to #760's original false signal."""
    session = _session(
        InputOutcome(
            reason=INPUT_SETTLE_CAP,
            ingested=True,
            submitted=True,
            submit_confirmed=False,
            waited_ms=3000,
        )
    )
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "steer"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["submit_confirmed"] is False
    assert body["reason"] == INPUT_SETTLE_CAP


def test_input_unknown_session_returns_404(monkeypatch):
    monkeypatch.setattr(server.manager, "get", lambda sid: None)
    client = TestClient(server.app)

    resp = client.post("/sessions/no-such/input", json={"data": "hello"})

    assert resp.status_code == 404
