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
    startedAt: [], stopped: 0,
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
    start(t) {
      window.__audioLog.started += 1;
      window.__audioLog.startedAt.push(t);
      window.__lastNode = this;
    }
    stop() { window.__audioLog.stopped += 1; }
  }
  class FakeAudioContext {
    constructor() {
      this.currentTime = 0; this.destination = {};
      window.__audioLog.contexts += 1;
      window.__lastCtx = this;
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
  // Make document.visibilityState scriptable so a test can simulate a
  // screen-lock (hidden) → unlock (visible) cycle and drive visibilitychange.
  window.__setVisibility = (state) => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true, get: () => state,
    });
    document.dispatchEvent(new Event('visibilitychange'));
  };
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


def test_screen_lock_reschedules_pending_buffers(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """A screen-lock mid-playback (#248) must not drop the read-aloud tail.

    iOS keeps the AudioContext `running` (currentTime advances) but suspends
    output during a lock, so buffers scheduled on the absolute timeline whose
    start elapsed during the lock are "started in the past" and dropped. Simulate
    the lock as a `visibilitychange` hidden→visible cycle around a forced
    `currentTime` jump: every still-pending buffer must be re-scheduled from the
    new clock position (no buffer lost) and playback must not finish early."""
    authed_page.add_init_script(_AUDIO_MOCK)
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "async () => {"
        "  await window.__readback.speakHub('Reading a long reply.', {});"
        "  const before = window.__audioLog.started;"
        "  const ctx = window.__lastCtx;"
        "  window.__setVisibility('hidden');"       # lock: capture audible position
        "  ctx.currentTime = 100;"                  # clock advanced during the lock
        "  window.__setVisibility('visible');"      # unlock: re-anchor the tail
        "  const after = window.__audioLog.started;"
        "  const reanchored = window.__audioLog.startedAt.slice(before);"
        "  return {"
        "    before, after,"
        "    reanchored,"
        "    pressed: document.getElementById('terminalSpeak')"
        "               .getAttribute('aria-pressed'),"
        "  };"
        "}"
    )
    # The un-played tail is re-scheduled after the lock — nothing silently dropped.
    assert got["before"] >= 1
    assert got["after"] > got["before"]
    assert got["reanchored"]
    # The re-anchored buffers start at/after the post-lock clock (100), never in
    # the past where iOS would skip them.
    assert all(t >= 100 for t in got["reanchored"])
    # Playback is still in progress — the tail was preserved, not finished early.
    assert got["pressed"] == "true"


def test_chunk_for_hub_bounds_long_text(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """chunkForHub() keeps every segment under the per-request budget and loses
    no text (#254). A short reply stays a single segment (one request, unchanged);
    a long reply splits into several bounded segments whose concatenation still
    covers the whole input."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "() => {"
        "  const short = 'Build is green.';"
        "  const long = Array.from({length: 22}, (_, i) =>"
        "    `Segment number ${i} explains a distinct part of the deployment plan in some detail.`"
        "  ).join(' ');"
        "  const c = window.__readback;"
        "  return {"
        "    shortSegs: c.chunkForHub(short),"
        "    long: long,"
        "    segs: c.chunkForHub(long),"
        "  };"
        "}"
    )
    # A short reply is a single segment — one POST, identical to before.
    assert got["shortSegs"] == ["Build is green."]
    # The long reply is over the hub's ~900-char single-request cap and splits.
    assert len(got["long"]) > 1500
    segs = got["segs"]
    assert len(segs) >= 2
    # Every segment is bounded (JS budget HUB_SEGMENT_CHARS = 700; allow slack).
    assert all(0 < len(s) <= 760 for s in segs)
    # Nothing dropped: the concatenated segments cover essentially the whole reply
    # (only inter-segment join spaces are trimmed at boundaries).
    assert sum(len(s) for s in segs) >= int(0.95 * len(got["long"]))


def test_long_reply_issues_multiple_bounded_requests(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The acceptance for #254: a verbatim reply longer than the hub's per-request
    cap (~49.6 s of speech ≈ ~900 chars) must NOT be silently truncated. The hub
    path splits it into several bounded /api/tts/speak requests streamed
    back-to-back on ONE Web Audio timeline, so the total synthesized audio tracks
    the input length instead of flatlining at the single-request cap. Before this
    fix the verbatim path issued exactly one POST and lost everything past ~49.6 s.
    """
    authed_page.add_init_script(_AUDIO_MOCK)
    captured: list[str] = []

    def _capture(route) -> None:  # type: ignore[no-untyped-def]
        try:
            captured.append((route.request.post_data_json or {}).get("text", ""))
        except Exception:
            captured.append(route.request.post_data or "")
        route.fulfill(
            status=200, content_type="audio/L16",
            headers={"X-Sample-Rate": "24000"}, body=_PCM,
        )

    authed_page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"available": true}'
        ),
    )
    authed_page.route("**/api/tts/speak", _capture)
    _open_terminal(authed_page, base_url, launched_pty_session)

    # ~1800 chars ≈ 98 s of Orpheus speech at the measured ~18 chars/s — well past
    # the single-request ~49.6 s / ~900-char cap, so a non-chunking path truncates.
    long_reply = " ".join(
        f"Segment number {i} explains a distinct part of the deployment plan in some detail."
        for i in range(22)
    )
    assert len(long_reply) > 1500

    got = authed_page.evaluate(
        "async (t) => { const ok = await window.__readback.speakHub("
        "  t, { token: 'bt', terminalToken: 'tt' });"
        " return Object.assign({ ok }, window.__audioLog); }",
        long_reply,
    )
    assert got["ok"] is True
    # MULTIPLE bounded synth requests were issued — not one capped request.
    assert len(captured) >= 2
    # Each request stays within the segment budget (~700 chars) so the hub never
    # hits its 4096-token / ~49.6 s ceiling on any single request.
    assert all(0 < len(t) <= 760 for t in captured)
    # The whole reply was synthesized: total characters across the requests track
    # the input length rather than flatlining at the first ~900-char cap.
    assert sum(len(t) for t in captured) >= int(0.95 * len(long_reply))
    # All segments streamed onto ONE timeline (one AudioContext), back-to-back,
    # with at least one scheduled buffer per segment.
    assert got["contexts"] == 1
    assert got["started"] >= 2
