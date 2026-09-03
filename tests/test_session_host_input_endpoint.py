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

#763: a submit handed to the deferred watcher answers 202 — accepted, not
completed — rather than a 200 that would read as a finished delivery.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server
from src import session_client, session_host_input
from src.session_host_input import (
    INPUT_DEFERRED,
    INPUT_DROPPED,
    INPUT_NOT_INGESTED,
    INPUT_OK,
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
    """An ingested payload whose submit is not confirmed still returns 200 —
    but must never look like a confirmed delivery, or the caller is back to
    #760's original false signal."""
    session = _session(
        InputOutcome(
            reason=INPUT_UNVERIFIED,
            ingested=True,
            submitted=True,
            submit_confirmed=None,
            waited_ms=3000,
        )
    )
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "steer"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["submit_confirmed"] is None
    assert body["reason"] == INPUT_UNVERIFIED


def test_deferred_submit_returns_202_not_a_completed_200(monkeypatch):
    """#763: the payload is in the composer but the agent was still working,
    so the CR is with the background watcher rather than fired blind. 202 is
    the honest status for that — accepted, not completed — and the body says
    ``deferred`` so a caller knows to follow ``last_input`` for the verdict."""
    session = _session(
        InputOutcome(
            reason=INPUT_DEFERRED, ingested=True, deferred=True, waited_ms=3000
        )
    )
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)

    resp = client.post("/sessions/sid/input", json={"data": "steer"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["ok"] is True
    assert body["reason"] == INPUT_DEFERRED
    assert body["deferred"] is True
    # Delivered (the payload really is in the composer), but the submit is
    # explicitly not established yet — never folded into the passing state.
    assert body["delivered"] is True
    assert body["submit_confirmed"] is None


def test_deferred_reason_constant_does_not_drift_across_the_process_boundary():
    """``src.session_client`` restates this one reason rather than importing
    it, because the webapp process never imports the PTY module. Pin the two
    definitions equal so the 202 mirror can't silently stop matching."""
    assert session_client.INPUT_DEFERRED == session_host_input.INPUT_DEFERRED


def test_input_unknown_session_returns_404(monkeypatch):
    monkeypatch.setattr(server.manager, "get", lambda sid: None)
    client = TestClient(server.app)

    resp = client.post("/sessions/no-such/input", json={"data": "hello"})

    assert resp.status_code == 404
