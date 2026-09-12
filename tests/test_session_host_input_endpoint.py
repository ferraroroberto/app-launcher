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

#929: ``delivered`` is never more optimistic than ``submitted`` — a paste the
watcher gave up on (``defer_timeout``) reported ``delivered: true`` while its
own sibling fields said the submit never happened.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.session_host import server
from src import session_client, session_host_input
from src.session_host import PtySession
from src.session_host_input import (
    INPUT_DEFER_TIMEOUT,
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
    # The payload is in the composer but has not reached the agent: the
    # submit is still with the watcher, so this is not a delivery yet (#929)
    # — and the pending state is named, never folded into the passing one.
    assert body["delivered"] is False
    assert body["submitted"] is False
    assert body["submit_state"] == "pending"
    assert body["submit_confirmed"] is None


def test_defer_timeout_is_reported_as_not_delivered_end_to_end(monkeypatch):
    """#929, driven for real: no mocked outcome, no fake clock.

    A real ``PtySession`` behind the real session-host route, a busy agent
    whose composer keeps repainting the paste, and the real watcher thread
    running out its (shortened) window. Before #929 both the 202 and the
    watcher's ``defer_timeout`` verdict on ``last_input`` said
    ``delivered: true`` next to ``submitted: false`` — the three stranded
    fleet-chief briefs of 2026-09-12.
    """
    monkeypatch.setattr(session_host_input, "_BULK_FLOOR_MS", 50)
    monkeypatch.setattr(session_host_input, "_BULK_CAP_MS", 300)
    monkeypatch.setattr(session_host_input, "_DEFER_CAP_MS", 800)
    session = PtySession(
        session_id="sid-929",
        project_dir=r"C:\stub",
        name="claude",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(name="PtyProcess"),
    )
    payload = "CHIEF - standing brief, read before doing anything else. " * 20
    stop = threading.Event()

    def _busy_agent() -> None:
        # The composer shows the paste on every frame while the spinner never
        # stops — the stream is never quiet for _DEFER_QUIET_MS.
        while not stop.is_set():
            frame = "\x1b[2K\r> [Pasted text #1 +20 lines]  ✻ Working…"
            with session._ring_lock:
                session._ring += frame
                session._output_total += len(frame)
            session._last_output_at = time.time()
            stop.wait(0.05)

    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    client = TestClient(server.app)
    pump = threading.Thread(target=_busy_agent, daemon=True)
    pump.start()
    try:
        resp = client.post(
            "/sessions/sid-929/input", json={"data": payload, "submit": True}
        )
        assert resp.status_code == 202
        posted = resp.json()
        assert posted["reason"] == INPUT_DEFERRED
        assert posted["delivered"] is False

        deadline = time.time() + 15
        last_input = None
        while time.time() < deadline:
            last_input = client.get("/sessions/sid-929").json()["last_input"]
            if last_input and last_input["reason"] != INPUT_DEFERRED:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        pump.join(timeout=2)

    assert last_input is not None
    assert last_input["reason"] == INPUT_DEFER_TIMEOUT
    # The decisive pair: the object must not disagree with itself.
    assert last_input["submitted"] is False
    assert last_input["delivered"] is False
    assert last_input["submit_state"] == "not_submitted"
    assert "\r" not in [c.args[0] for c in session._pty.write.call_args_list]


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
