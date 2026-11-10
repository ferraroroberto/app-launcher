"""Chat-mode read-aloud (issue #988, Step 9/10 of #979).

Read-aloud today reads the live xterm scrollback via a Claude-Code-shaped
colour classifier (terminal-readback.js), so it only ever works in Terminal
mode for that one agent. In Chat mode — and for a detached session, which can
only ever show Chat — the reply is already available as structured text from
the harness's own history file, so Read aloud reads the newest transcript entry
instead: harness-agnostic, and it works for a detached session too (its ⋮
menu rows are decided from Chat mode as well as from Terminal mode's attach
path).

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

from tests.e2e._audio_mock import AUDIO_MOCK
from tests.e2e._readaloud_menu import read_aloud_item, wait_for_hub_probe
from tests.e2e.fixtures.ui import open_session_row, stub_session_mirror

pytestmark = pytest.mark.smoke

_SID = "sid-chat-readaloud-988"
_SPEAK_RE = re.compile(r".*/api/tts/speak$")


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
    # #1025: the row's gear (which opened Chat in one tap) is gone, so Chat is
    # reached the way a finger reaches it — tap the row, then the bar's Chat
    # segment. A detached row already lands in Chat; a full-control row opens
    # in Terminal, and on the Chromium projection its tap would mirror to a PC
    # window (#282) without the stub.
    stub_session_mirror(page)
    open_session_row(page, row, mode="chat")
    expect(page.locator("#terminalOverlay")).to_be_visible()


def _click_speak_and_capture_text(page: Page) -> str:
    """Pick "Read aloud" from the ⋮ menu (once the hub probe has settled, so the
    row is there to tap), then wait for the resulting /api/tts/speak POST and
    return its ``text`` field."""
    wait_for_hub_probe(page)
    with page.expect_request(_SPEAK_RE) as req_info:
        read_aloud_item(page).click()
    return (req_info.value.post_data_json or {}).get("text", "")


def test_detached_session_reads_the_newest_entry_and_a_capped_one_uncapped(
    authed_page: Page, base_url: str
) -> None:
    """The core ask: a detached (no-PTY) session gets a working Read aloud row
    in the ⋮ menu, and it reads the transcript's newest assistant entry.

    Then a capped newest entry upgrades to the full text via /transcript/entry
    before speaking — never read out silently shortened (#979 decision log).
    A reply this long is chunked into several bounded hub requests
    (terminal-speech.js), so the proof is the *sum* of every request's text:
    capped at 12,000 chars would stay <= that; the uncapped 20,000-char entry
    must push the total well past it.
    """
    body = {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-09-17T10:01:00Z", "offset": 0,
             "text": "status?", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-09-17T10:01:05Z", "offset": 4096,
             "text": "All green, ship it.", "truncated": False, "sidechain": False},
        ],
    }
    full_reply = "R" * 20_000
    capped_reply = full_reply[:12_000]
    captured: list = []
    _mock_sessions_list(authed_page, kind="remote")
    # The route dumps ``body`` per request, so editing it changes the next read.
    _mock_transcript(authed_page, body)
    _mock_transcript_entry(authed_page, full_reply)
    _mock_tts_capturing(authed_page, captured)
    authed_page.add_init_script(AUDIO_MOCK)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    _open_chat(authed_page, _row(authed_page))
    # No Terminal segment for a detached session — its read-aloud rows are
    # decided from Chat mode too, not only off attachTerminalPane.
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-disabled", "true")
    text = _click_speak_and_capture_text(authed_page)
    assert text == "All green, ship it.", text
    expect(authed_page.locator(".toast")).to_contain_text("Reading:")
    expect(authed_page.locator("#terminalMenu")).to_have_attribute("data-speaking", "false")

    # The newest entry is now a capped one.
    body["entries"] = [
        {"kind": "assistant", "timestamp": "2026-09-17T10:01:05Z", "offset": 4096,
         "text": capped_reply, "truncated": True, "sidechain": False},
    ]
    captured.clear()
    authed_page.reload(wait_until="domcontentloaded")
    _open_chat(authed_page, _row(authed_page))
    wait_for_hub_probe(authed_page)
    read_aloud_item(authed_page).click()
    # The ⋮ button returns to idle once every chunked request has streamed and
    # finished playing — a bounded proxy for "all requests captured".
    expect(authed_page.locator("#terminalMenu")).to_have_attribute("data-speaking", "false")

    total = sum(len(t) for t in captured)
    assert len(captured) >= 2, f"expected the long reply to split into multiple requests, got {captured}"
    assert total > len(capped_reply), (
        f"total spoken chars ({total}) never exceeded the capped entry's length "
        f"({len(capped_reply)}) — looks like the capped text was read, not the uncapped entry"
    )
    # Matches the tolerance test_chunk_for_hub_bounds_long_text uses: a little
    # inter-segment trimming at boundaries is expected, wholesale truncation isn't.
    assert total >= int(0.95 * len(full_reply)), (captured and total, len(full_reply))


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
    authed_page.add_init_script(AUDIO_MOCK)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    _open_chat(authed_page, row)
    text = _click_speak_and_capture_text(authed_page)
    assert text == "Chat-mode reply.", text
