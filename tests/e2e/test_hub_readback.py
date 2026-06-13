"""Regression pin for hub read-aloud (issue #203).

The 🔊 button gains a second voice: when the local-llm-hub is reachable, the
reply is synthesized through the hub's Orpheus voice instead of the on-device
Web Speech voice. ``speakHub()`` POSTs the reply to ``/api/tts/speak`` (bearer +
passkey headers, since ``<audio src>`` can't carry them) and plays the returned
WAV through an ``<audio>`` element; ``probeHub()`` caches hub reachability so the
click can pick the path; ``cancelHub()`` stops in-flight audio on a re-press /
tab-leave / new dictation.

``<audio>`` and the hub endpoints are stubbed (no live hub on :8000, no real
autoplay in headless WebKit). The JS hub surface is driven through the
``window.__readback`` seam, mirroring ``test_voice_readback.py``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Stub the <audio> element: record every play()/pause() and every src set so
# the test can assert the unlock-then-blob playback sequence without a real
# audio pipeline (headless WebKit has none, and autoplay is gated).
_AUDIO_MOCK = """
(() => {
  window.__audioLog = { played: 0, paused: 0, srcs: [] };
  class FakeAudio {
    constructor(src) {
      this._src = src || '';
      this.onended = null; this.onerror = null;
      if (src) window.__audioLog.srcs.push(src);
    }
    get src() { return this._src; }
    set src(v) { this._src = v; if (v) window.__audioLog.srcs.push(v); }
    play() { window.__audioLog.played += 1; return Promise.resolve(); }
    pause() { window.__audioLog.paused += 1; }
    removeAttribute() { this._src = ''; }
    load() {}
  }
  Object.defineProperty(window, 'Audio', {
    configurable: true, writable: true, value: FakeAudio,
  });
})()
"""

_WAV = b"RIFF$\x00\x00\x00WAVEfmt loud-bytes"


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)


def _route_hub(page: Page, *, available: bool = True, speak_status: int = 200,
               wav: bytes = _WAV) -> None:
    page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"available": %s}' % ("true" if available else "false"),
        ),
    )
    page.route(
        "**/api/tts/speak",
        lambda route: route.fulfill(
            status=speak_status, content_type="audio/wav", body=wav,
        ),
    )


def test_probe_caches_hub_availability(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """probeHub() reflects the /api/tts/health verdict; isHubAvailable() caches it."""
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "async () => { const ok = await window.__readback.probeHub({});"
        " return { ok, cached: window.__readback.isHubAvailable() }; }"
    )
    assert got == {"ok": True, "cached": True}


def test_speak_hub_plays_streamed_wav(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """speakHub() unlocks the <audio> element, POSTs to /api/tts/speak, then
    plays the returned WAV blob — two play()s (silent unlock + real blob) and a
    blob: src swapped in after the empty-WAV unlock clip."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "async () => { const ok = await window.__readback.speakHub('Build is green.', {});"
        " return { ok, played: window.__audioLog.played,"
        "          blob: window.__audioLog.srcs.some(s => s.startsWith('blob:')) }; }"
    )
    assert got["ok"] is True
    assert got["played"] >= 2           # silent unlock + real blob playback
    assert got["blob"] is True          # the streamed WAV became a blob: src


def test_speak_hub_empty_body_rejects_for_fallback(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """A zero-byte body (hub errored after the 200 committed) makes speakHub()
    reject, so the click handler can fall back to Web Speech."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True, wav=b"")
    _open_terminal(authed_page, base_url, launched_pty_session)
    rejected = authed_page.evaluate(
        "async () => { try { await window.__readback.speakHub('hi', {}); return false; }"
        " catch (_) { return true; } }"
    )
    assert rejected is True


def test_cancel_hub_stops_audio(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """cancelHub() pauses in-flight audio and resets the button to idle."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "async () => { await window.__readback.speakHub('Reading on.', {});"
        " window.__readback.cancelHub();"
        " return { paused: window.__audioLog.paused,"
        "          pressed: document.getElementById('terminalSpeak')"
        "                     .getAttribute('aria-pressed') }; }"
    )
    assert got["paused"] >= 1
    assert got["pressed"] == "false"
