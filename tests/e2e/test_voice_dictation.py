"""Regression pin for issues #165 / #168 (compose-bar voice dictation).

The feature: a ``🎤`` button inside the compose bar records the mic and
drops the transcript into the compose ``<textarea>`` for review — never
straight into the PTY. The preferred flow (#168) streams: create a voice
session, POST chunks, and revise the dictated span live from a Server-Sent
-Events stream of rolling ``partial`` transcripts, settling on ``finish``.
If streaming setup fails it falls back to the #165 single-shot ``/upload``.

The harness connects from loopback, so the terminal opens as the PC mirror
(``isMirror`` true) and the compose bar / record button start hidden. As in
``test_compose_bar.py`` we un-hide the compose toggle and drive the real
handlers — the record/transcribe logic is not mirror-gated, only the
buttons' visibility is.

``MediaRecorder`` + ``getUserMedia`` aren't available/grantable in headless
WebKit, so both are stubbed via an init script; the transcribe endpoints
(create / events SSE / chunk / finish, and the single-shot fallback) are
mocked with ``page.route`` so no live voice-transcriber on :8443 is needed.
"""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# Stub getUserMedia + MediaRecorder before the SPA loads. The fake recorder
# fires one `dataavailable` then `stop` synchronously on .stop(), mirroring
# the real start→stop→blob flow the handler depends on. It accepts (and
# ignores) the timeslice arg the streaming path passes to .start().
_MEDIA_MOCK = """
(() => {
  navigator.mediaDevices = navigator.mediaDevices || {};
  navigator.mediaDevices.getUserMedia = async () => ({
    getTracks: () => [{ stop: () => {} }],
  });
  class FakeRecorder {
    constructor(stream, opts) {
      this.stream = stream;
      this.mimeType = (opts && opts.mimeType) || 'audio/webm';
      this.state = 'inactive';
      this._listeners = {};
    }
    addEventListener(ev, cb) { this._listeners[ev] = cb; }
    start(_timeslice) { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      const da = this._listeners['dataavailable'];
      if (da) da({ data: new Blob(['fake-audio'], { type: this.mimeType }) });
      const st = this._listeners['stop'];
      if (st) st();
    }
  }
  FakeRecorder.isTypeSupported = () => true;
  window.MediaRecorder = FakeRecorder;
})()
"""

_PARTIAL = "live partial text"
_FINAL = "final-{regress} transcript"


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=10_000,
    )


def _open_compose_with_record(page: Page) -> None:
    """Un-hide + open the compose bar, then un-hide the record button."""
    page.evaluate("document.getElementById('terminalCompose').hidden = false")
    page.locator("#terminalCompose").click()
    expect(page.locator("#terminalComposeBar")).to_be_visible()
    page.evaluate("document.getElementById('terminalRecord').hidden = false")


def test_record_button_lives_in_compose_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The 🎤 button is a child of the compose bar, beside ➤ Send."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    record = authed_page.locator("#terminalComposeBar #terminalRecord")
    expect(record).to_have_count(1)


def test_streamed_partials_then_final(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """🎤 → live SSE partial appears, → stop settles the final transcript (#168)."""
    sid = launched_pty_session
    authed_page.add_init_script(_MEDIA_MOCK)
    authed_page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"session_id": "vt-1"}',
        ),
    )
    authed_page.route(
        "**/api/transcribe/sessions/vt-1/events*",
        lambda route: route.fulfill(
            status=200, content_type="text/event-stream",
            body='event: partial\ndata: {"version":1,"transcript":"%s"}\n\n' % _PARTIAL,
        ),
    )
    authed_page.route(
        "**/api/transcribe/sessions/vt-1/chunk",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"raw_bytes": 9}',
        ),
    )
    authed_page.route(
        "**/api/transcribe/sessions/vt-1/finish",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"transcript": "%s", "language": "en"}' % _FINAL,
        ),
    )
    _open_terminal(authed_page, base_url, sid)
    _open_compose_with_record(authed_page)

    record = authed_page.locator("#terminalRecord")
    record.click()
    expect(record).to_have_class(re.compile(r"\brecording\b"))
    # The SSE partial revises the dictated span live, before stop.
    expect(authed_page.locator("#terminalComposeInput")).to_have_value(
        re.compile(re.escape(_PARTIAL)), timeout=10_000
    )

    record.click()
    # finish() settles the canonical transcript into the same span.
    expect(authed_page.locator("#terminalComposeInput")).to_have_value(
        _FINAL, timeout=10_000
    )
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()


def test_single_shot_fallback_when_no_session(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Create-session failure falls back to the #165 single-shot path."""
    sid = launched_pty_session
    authed_page.add_init_script(_MEDIA_MOCK)
    # Streamed create fails → handler must fall back to /api/transcribe.
    authed_page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(status=503, body="nope"),
    )
    authed_page.route(
        "**/api/transcribe",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"transcript": "%s", "language": "en"}' % _FINAL,
        ),
    )
    _open_terminal(authed_page, base_url, sid)
    _open_compose_with_record(authed_page)

    record = authed_page.locator("#terminalRecord")
    record.click()
    record.click()  # stop → buffered blob → single-shot POST

    expect(authed_page.locator("#terminalComposeInput")).to_have_value(
        _FINAL, timeout=10_000
    )


def test_send_refuses_while_dictation_still_finishing(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    """➤ Send is a no-op while a stopped dictation is still finalizing (#489).

    finishStreaming() awaits /finish before settling the canonical
    transcript into the tracked ``[_dictStart, _dictStart+_dictLen)`` span.
    Before this fix, a Send tapped in that window read+submitted the stale
    partial and cleared the textarea; the late ``renderDictation()`` call
    then wrote the final transcript into the (now emptied) span via
    ``setRangeText``, which clamps to offset 0 on a shorter string — so the
    final transcript landed unsent in an already-"sent" box instead of Send
    ever seeing it. A long dictation widens the finalize round trip, giving
    this a real window to bite — reproduced here by holding the mocked
    ``/finish`` response open until the test explicitly resolves it.
    """
    sid = launched_pty_session
    authed_page.add_init_script(_MEDIA_MOCK)
    authed_page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"session_id": "vt-1"}',
        ),
    )
    # EventSource auto-reconnects once this single-chunk response completes.
    # Fulfilling only the first hit and leaving reconnects pending (never
    # resolved) keeps a stray reconnect from re-delivering the same partial
    # and re-populating the textarea while /finish is deliberately held open
    # below — which would mask the very race this test exists to catch.
    _events_calls = {"n": 0}

    def _events_route(route):
        _events_calls["n"] += 1
        if _events_calls["n"] == 1:
            route.fulfill(
                status=200, content_type="text/event-stream",
                body='event: partial\ndata: {"version":1,"transcript":"%s"}\n\n' % _PARTIAL,
            )

    authed_page.route("**/api/transcribe/sessions/vt-1/events*", _events_route)
    authed_page.route(
        "**/api/transcribe/sessions/vt-1/chunk",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"raw_bytes": 9}',
        ),
    )
    # Held open (never fulfilled inside the handler) so the test controls
    # exactly when the finalize round trip resolves — a fixed sleep can't
    # reliably outlast Playwright's own route-dispatch latency.
    held_finish = {}
    authed_page.route(
        "**/api/transcribe/sessions/vt-1/finish",
        lambda route: held_finish.__setitem__("route", route),
    )

    _open_terminal(authed_page, base_url, sid)
    _open_compose_with_record(authed_page)

    record = authed_page.locator("#terminalRecord")
    record.click()
    expect(authed_page.locator("#terminalComposeInput")).to_have_value(
        re.compile(re.escape(_PARTIAL)), timeout=10_000
    )
    record.click()  # stop -> finishStreaming() begins; /finish is held pending

    for _ in range(100):
        if "route" in held_finish:
            break
        time.sleep(0.05)
    assert "route" in held_finish, "finishStreaming() never called /finish"

    # Tap Send while the finalize is still in flight.
    authed_page.locator("#terminalComposeSend").click()

    # The refused Send must not have touched the textarea — it should still
    # hold the pre-finalize partial, unchanged, not cleared out from under
    # the in-flight render.
    expect(authed_page.locator("#terminalComposeInput")).to_have_value(
        re.compile(re.escape(_PARTIAL))
    )

    # Resolve the finalize round trip; the canonical transcript settles in.
    held_finish["route"].fulfill(
        status=200, content_type="application/json",
        body='{"transcript": "%s", "language": "en"}' % _FINAL,
    )
    expect(authed_page.locator("#terminalComposeInput")).to_have_value(
        _FINAL, timeout=10_000
    )

    # Dictation is idle now — Send works normally and reaches the real PTY.
    authed_page.locator("#terminalComposeSend").click()
    expect(authed_page.locator("#terminalComposeInput")).to_have_value("")
    assert wait_for_session_log(authed_page, sid, _FINAL), (
        "the settled transcript never reached the live PTY session"
    )
