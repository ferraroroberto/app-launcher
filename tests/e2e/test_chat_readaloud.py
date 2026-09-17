"""Chat-mode read-aloud (issue #988, Step 9/10 of #979).

Read-aloud today reads the live xterm scrollback via a Claude-Code-shaped
colour classifier (terminal-readback.js), so it only ever works in Terminal
mode for that one agent. In Chat mode — and for a detached session, which can
only ever show Chat — the reply is already available as structured text from
the harness's own history file, so 🔊 reads the newest transcript entry
instead: harness-agnostic, and it finally works for a detached session (the
🔊 button never revealed for one before this fix, since it was only wired to
Terminal mode's attach path).

A capped entry (`truncated: true`) is upgraded to the full text via the
uncapped `/transcript/entry?offset=` route before speaking — the same
truncation problem the copy button (#985) solved, applied here so a long
reply is never read out silently shortened (#979's decision log, Step 6).

Every non-deterministic boot fetch is stubbed before `goto()` (#510).
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_SID = "sid-chat-readaloud-988"
_SPEAK_RE = re.compile(r".*/api/tts/speak$")

# Stub Web Audio so prepareHub()/speakHubInto() can run without a real audio
# pipeline (headless WebKit has none). Mirrors test_summarize_readback.py.
_AUDIO_MOCK = """
(() => {
  window.__audioLog = { contexts: 0, resumed: 0, buffers: 0, started: 0 };
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
  // scheduleHubBuffer() (issue #599) routes every node through a GainNode for
  // a click-avoiding fade; the fake just needs the shape (connect + a gain
  // param with the AudioParam automation methods it calls).
  class FakeGainNode {
    constructor() {
      this.gain = {
        setValueAtTime() {}, linearRampToValueAtTime() {},
      };
    }
    connect() {}
  }
  class FakeAudioContext {
    constructor() {
      this.currentTime = 0; this.destination = {};
      window.__audioLog.contexts += 1;
    }
    resume() { window.__audioLog.resumed += 1; return Promise.resolve(); }
    createBuffer(ch, len, sr) {
      window.__audioLog.buffers += 1; return new FakeAudioBuffer(ch, len, sr);
    }
    createBufferSource() { return new FakeNode(); }
    createGain() { return new FakeGainNode(); }
    close() { return Promise.resolve(); }
  }
  for (const name of ['AudioContext', 'webkitAudioContext']) {
    Object.defineProperty(window, name, {
      configurable: true, writable: true, value: FakeAudioContext,
    });
  }
})()
"""


def _mock_sessions_list(page: Page, *, kind: str = "pty", agent: str = "claude") -> None:
    def _handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": _SID,
                "kind": kind,
                "agent": agent,
                "project_dir": "E:/automation/readaloudproj",
                "name": "readaloudproj",
                "alive": True,
                "started_at": "2026-09-17T10:00:00Z",
                "live_title": "",
                "prompt_title": "",
                "manual_title": "Read-aloud demo",
            }]}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions$"), _handler)


def _mock_transcript(page: Page, body: dict) -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body=_json.dumps(body)),
    )


def _mock_transcript_entry(page: Page, text: str) -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/transcript/entry(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"available": True, "reason": None, "session_id": _SID,
                               "text": text, "truncated": False}),
        ),
    )


def _mock_tts(page: Page) -> None:
    page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"available": true}'
        ),
    )
    page.route(
        "**/api/tts/speak",
        lambda route: route.fulfill(
            status=200, content_type="audio/L16",
            headers={"X-Sample-Rate": "24000"}, body=b"\xc2\xff\xc0\xff",
        ),
    )


def _mock_tts_capturing(page: Page, captured: list) -> None:
    """Like _mock_tts, but records every /api/tts/speak request's ``text`` —
    a long reply is chunked into several bounded requests (terminal-speech.js
    chunkForHub), so a single-request assertion can't tell a capped read from
    an uncapped one; the *sum* across requests can."""
    page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"available": true}'
        ),
    )

    def _capture(route):
        try:
            captured.append((route.request.post_data_json or {}).get("text", ""))
        except Exception:
            captured.append(route.request.post_data or "")
        route.fulfill(
            status=200, content_type="audio/L16",
            headers={"X-Sample-Rate": "24000"}, body=b"\xc2\xff\xc0\xff",
        )

    page.route("**/api/tts/speak", _capture)


def _row(page: Page):
    return page.locator(f'#sessionsList li[data-session-id="{_SID}"]')


def _open_chat(page: Page, row) -> None:
    row.locator(".session-gear").click()
    row.locator('button[aria-label="Open chat"]').click()
    overlay = page.locator("#terminalOverlay")
    expect(overlay).to_be_visible()
    expect(overlay).to_have_attribute("data-mode", "chat")


def _click_speak_and_capture_text(page: Page) -> str:
    """Tap 🔊 and pick "Read aloud" from the popover (the mocked hub always
    resolves reachable, so the popover deterministically opens — same wait
    test_summarize_readback.py uses for the probe to clear `hidden`), then
    wait for the resulting /api/tts/speak POST and return its ``text`` field."""
    speak = page.locator("#terminalSpeak")
    expect(speak).to_be_visible()
    page.wait_for_selector(
        '#terminalSpeakPopover [data-action="summarize"]:not([hidden])',
        state="attached", timeout=10_000,
    )
    with page.expect_request(_SPEAK_RE) as req_info:
        speak.click()
        page.locator('#terminalSpeakPopover [data-action="read"]').click()
    return (req_info.value.post_data_json or {}).get("text", "")


def test_detached_session_reveals_speak_button_and_reads_transcript(
    authed_page: Page, base_url: str
) -> None:
    """The core ask: a detached (no-PTY) session gets a working 🔊 for the
    first time, and it reads the transcript's newest assistant entry."""
    _mock_sessions_list(authed_page, kind="remote")
    _mock_transcript(authed_page, {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-09-17T10:01:00Z", "offset": 0,
             "text": "status?", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-09-17T10:01:05Z", "offset": 4096,
             "text": "All green, ship it.", "truncated": False, "sidechain": False},
        ],
    })
    _mock_tts(authed_page)
    authed_page.add_init_script(_AUDIO_MOCK)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    _open_chat(authed_page, row)
    # No Terminal segment for a detached session — before this fix the 🔊
    # button never revealed at all, since it only ran off attachTerminalPane.
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-disabled", "true")
    text = _click_speak_and_capture_text(authed_page)
    assert text == "All green, ship it.", text
    expect(authed_page.locator(".toast")).to_contain_text("Reading:")


def test_full_control_session_in_chat_mode_reads_transcript_not_terminal(
    authed_page: Page, base_url: str
) -> None:
    """A PTY session opened straight into Chat mode also reads the transcript
    — the provider follows the pane that is showing, not PTY presence."""
    _mock_sessions_list(authed_page, kind="pty", agent="claude")
    _mock_transcript(authed_page, {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "assistant", "timestamp": "2026-09-17T10:01:05Z", "offset": 4096,
             "text": "Chat-mode reply.", "truncated": False, "sidechain": False},
        ],
    })
    _mock_tts(authed_page)
    authed_page.add_init_script(_AUDIO_MOCK)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    _open_chat(authed_page, row)
    text = _click_speak_and_capture_text(authed_page)
    assert text == "Chat-mode reply.", text


def test_truncated_entry_reads_the_uncapped_text(authed_page: Page, base_url: str) -> None:
    """A capped newest entry upgrades to the full text via /transcript/entry
    before speaking — never read out silently shortened (#979 decision log).

    A reply this long is chunked into several bounded hub requests
    (terminal-speech.js), so the proof is the *sum* of every request's text:
    capped at 12,000 chars would stay <= that; the uncapped 20,000-char entry
    must push the total well past it.
    """
    full_reply = "R" * 20_000
    capped_reply = full_reply[:12_000]
    captured: list = []
    _mock_sessions_list(authed_page, kind="remote")
    _mock_transcript(authed_page, {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "assistant", "timestamp": "2026-09-17T10:01:05Z", "offset": 4096,
             "text": capped_reply, "truncated": True, "sidechain": False},
        ],
    })
    _mock_transcript_entry(authed_page, full_reply)
    _mock_tts_capturing(authed_page, captured)
    authed_page.add_init_script(_AUDIO_MOCK)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    _open_chat(authed_page, row)
    speak = authed_page.locator("#terminalSpeak")
    expect(speak).to_be_visible()
    authed_page.wait_for_selector(
        '#terminalSpeakPopover [data-action="summarize"]:not([hidden])',
        state="attached", timeout=10_000,
    )
    speak.click()
    authed_page.locator('#terminalSpeakPopover [data-action="read"]').click()
    # The button returns to idle once every chunked request has streamed and
    # finished playing — a bounded proxy for "all requests captured".
    expect(authed_page.locator("#terminalSpeak")).to_have_attribute("aria-pressed", "false")

    total = sum(len(t) for t in captured)
    assert len(captured) >= 2, f"expected the long reply to split into multiple requests, got {captured}"
    assert total > len(capped_reply), (
        f"total spoken chars ({total}) never exceeded the capped entry's length "
        f"({len(capped_reply)}) — looks like the capped text was read, not the uncapped entry"
    )
    # Matches the tolerance test_chunk_for_hub_bounds_long_text uses: a little
    # inter-segment trimming at boundaries is expected, wholesale truncation isn't.
    assert total >= int(0.95 * len(full_reply)), (captured and total, len(full_reply))
