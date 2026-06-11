"""Regression pin for issue #165 (compose-bar voice dictation).

The feature: a ``🎤`` button inside the compose bar records the mic, POSTs
the blob to ``/api/transcribe`` (which the webapp proxies to the sibling
voice-transcriber over loopback), and drops the returned transcript into
the compose ``<textarea>`` for review — never straight into the PTY.

The harness connects from loopback, so the terminal opens as the PC mirror
(``isMirror`` true) and the compose bar / record button start hidden. As in
``test_compose_bar.py`` we un-hide the compose toggle and drive the real
handlers — the record/transcribe logic is not mirror-gated, only the
buttons' visibility is.

``MediaRecorder`` + ``getUserMedia`` aren't available/grantable in headless
WebKit, so both are stubbed via an init script; ``/api/transcribe`` is
mocked with ``page.route`` so no live voice-transcriber on :8443 is needed.
This pins the wiring: click-to-record, the upload call, and the transcript
landing in the textarea.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# Stub getUserMedia + MediaRecorder before the SPA loads. The fake recorder
# fires one `dataavailable` then `stop` synchronously on .stop(), mirroring
# the real start→stop→blob flow the handler depends on.
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
    start() { this.state = 'recording'; }
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

_TRANSCRIPT = "voice-dictation-{regress}"


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=10_000,
    )


def test_record_button_lives_in_compose_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The 🎤 button is a child of the compose bar, beside ➤ Send."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    record = authed_page.locator("#terminalComposeBar #terminalRecord")
    expect(record).to_have_count(1)


def test_record_transcribes_into_compose_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """🎤 → stop → /api/transcribe transcript lands in the textarea, not the PTY."""
    sid = launched_pty_session
    authed_page.add_init_script(_MEDIA_MOCK)
    authed_page.route(
        "**/api/transcribe*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"transcript": "%s", "language": "en"}' % _TRANSCRIPT,
        ),
    )
    _open_terminal(authed_page, base_url, sid)

    # Un-hide + open the compose bar (mirror trick — see module docstring),
    # then un-hide the record button (status-gated in production).
    authed_page.evaluate(
        "document.getElementById('terminalCompose').hidden = false"
    )
    authed_page.locator("#terminalCompose").click()
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()
    authed_page.evaluate(
        "document.getElementById('terminalRecord').hidden = false"
    )

    # First tap starts recording (button flips to the stop glyph), second
    # tap stops → the fake recorder emits a blob → upload → transcript.
    record = authed_page.locator("#terminalRecord")
    record.click()
    expect(record).to_have_class(re.compile(r"\brecording\b"))
    record.click()

    expect(authed_page.locator("#terminalComposeInput")).to_have_value(
        _TRANSCRIPT, timeout=10_000
    )
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()
