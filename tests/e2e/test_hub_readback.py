"""Regression pin for hub read-aloud (issues #203, #206).

The 🔊 button gains a second voice: when the local-llm-hub is reachable, the
reply is synthesized through the hub's Orpheus voice instead of the on-device
Web Speech voice. For low time-to-first-audio (#206) ``speakHub()`` POSTs the
reply to ``/api/tts/speak``, which streams **headerless PCM16** as the hub
synthesizes, and plays it through the **Web Audio API** — read the streaming
fetch, convert int16→float32, and schedule ``AudioBufferSourceNode``s on an
``AudioContext`` resumed in the click gesture. ``probeHub()`` caches hub
reachability so the click can pick the path; ``cancelHub()`` closes the context
(silencing scheduled audio) on a re-press / tab-leave / new dictation.

The ``AudioContext`` and the hub endpoints are stubbed (no live hub on :8000,
no real audio pipeline in headless WebKit). The JS hub surface is driven
through the ``window.__readback`` seam, mirroring ``test_voice_readback.py``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Stub the Web Audio API: record AudioContext lifecycle + every scheduled buffer
# so the test can assert PCM was decoded and scheduled, without a real audio
# pipeline (headless WebKit has none). Shadows both the standard and webkit
# constructors so the SPA's `window.AudioContext || window.webkitAudioContext`
# resolves to the fake.
_AUDIO_MOCK = """
(() => {
  window.__audioLog = {
    contexts: 0, resumed: 0, buffers: 0, started: 0, closed: 0, samples: 0,
  };
  class FakeAudioBuffer {
    constructor(ch, len, sr) {
      this.length = len; this.sampleRate = sr;
      this.duration = len / Math.max(1, sr);
    }
    copyToChannel() {}
  }
  class FakeNode {
    constructor() { this.buffer = null; this.onended = null; }
    connect() {}
    start() { window.__audioLog.started += 1; window.__lastNode = this; }
  }
  class FakeAudioContext {
    constructor() {
      this.currentTime = 0; this.destination = {};
      window.__audioLog.contexts += 1;
    }
    resume() { window.__audioLog.resumed += 1; return Promise.resolve(); }
    createBuffer(ch, len, sr) {
      window.__audioLog.buffers += 1; window.__audioLog.samples += len;
      return new FakeAudioBuffer(ch, len, sr);
    }
    createBufferSource() { return new FakeNode(); }
    close() { window.__audioLog.closed += 1; return Promise.resolve(); }
  }
  for (const name of ['AudioContext', 'webkitAudioContext']) {
    Object.defineProperty(window, name, {
      configurable: true, writable: true, value: FakeAudioContext,
    });
  }
})()
"""

# A few whole int16 samples of PCM16 — enough for the pump loop to decode and
# schedule at least one buffer.
_PCM = b"\xc2\xff\xc0\xff\xc5\xff\xca\xff"


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)


def _route_hub(page: Page, *, available: bool = True, speak_status: int = 200,
               pcm: bytes = _PCM) -> None:
    page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"available": %s}' % ("true" if available else "false"),
        ),
    )
    # POST /api/tts/speak streams headerless PCM16 with an X-Sample-Rate header;
    # the client decodes it through the (stubbed) Web Audio API.
    page.route(
        "**/api/tts/speak",
        lambda route: route.fulfill(
            status=speak_status, content_type="audio/L16",
            headers={"X-Sample-Rate": "24000"}, body=pcm,
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


def test_speak_hub_plays_pcm_via_web_audio(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """speakHub() creates + resumes an AudioContext, POSTs to /api/tts/speak,
    then decodes the PCM stream and schedules it on the Web Audio timeline."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "async () => { const ok = await window.__readback.speakHub("
        "  'Build is green.', { token: 'bt', terminalToken: 'tt' });"
        " return Object.assign({ ok }, window.__audioLog); }"
    )
    assert got["ok"] is True
    assert got["contexts"] >= 1          # an AudioContext was created
    assert got["resumed"] >= 1           # resumed in the gesture for iOS autoplay
    assert got["buffers"] >= 1           # PCM decoded into ≥1 AudioBuffer
    assert got["started"] >= 1           # scheduled on the timeline


def test_speak_hub_finishes_on_last_buffer_end(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Playback finalizes on the LAST scheduled buffer's onended (the real end),
    not a timer computed from a possibly-drifted playHead — so a slower-than-
    realtime stream isn't cut off early (#206 follow-up). Firing the last node's
    onended resets the button to idle."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    pressed = authed_page.evaluate(
        "async () => { await window.__readback.speakHub('Reading on.', {});"
        " window.__lastNode.onended();"   # the final buffer drains
        " return document.getElementById('terminalSpeak')"
        "          .getAttribute('aria-pressed'); }"
    )
    assert pressed == "false"


def test_speak_hub_failure_rejects_for_fallback(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """A failed POST makes speakHub() reject, so the click handler can fall
    back to Web Speech."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True, speak_status=502)
    _open_terminal(authed_page, base_url, launched_pty_session)
    rejected = authed_page.evaluate(
        "async () => { try { await window.__readback.speakHub('hi', {}); return false; }"
        " catch (_) { return true; } }"
    )
    assert rejected is True


def test_cancel_hub_stops_audio(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """cancelHub() closes the AudioContext (silencing scheduled audio) and
    resets the button to idle."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "async () => { await window.__readback.speakHub('Reading on.', {});"
        " window.__readback.cancelHub();"
        " return { closed: window.__audioLog.closed,"
        "          pressed: document.getElementById('terminalSpeak')"
        "                     .getAttribute('aria-pressed') }; }"
    )
    assert got["closed"] >= 1
    assert got["pressed"] == "false"
