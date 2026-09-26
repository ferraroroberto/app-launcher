"""Issue #1120 — one Load older tap never ends with nothing visible.

A server page can hold no user or assistant turn at all: the reader's
per-request read ceiling stops it inside a run of huge tool results
(screenshot payloads), and with tool calls hidden — the default — such a page
used to prepend nothing the reader could see. The tap looked dead.

Pins the client half, independent of how the server sizes its pages:

  * one tap chains past turn-less pages until an older turn arrives;
  * a transcript that is only tool calls back to the file start ends on an
    honest "no older messages" line, not a button that silently vanishes;
  * the chain is bounded, and hitting the bound says what it loaded instead
    of looking like nothing happened.

Every boot fetch is stubbed before ``goto()`` (#510); the transcript stub
serves synthetic pages keyed on ``before``, and answers the #1050 live tail
(``after=``) with "unchanged" so it never adds entries mid-test.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import open_session_row, stub_session_mirror

pytestmark = pytest.mark.smoke

_SID = "sid-load-older-1120"


def _mock_sessions_list(page: Page) -> None:
    def _handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": _SID, "kind": "pty", "agent": "claude",
                "project_dir": "E:/automation/heavyproj", "name": "heavyproj",
                "alive": True, "started_at": "2026-09-21T10:00:00Z",
                "live_title": "", "prompt_title": "", "manual_title": "Heavy demo",
            }]}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions$"), _handler)


def _turn(kind: str, text: str) -> dict:
    return {"kind": kind, "timestamp": "2026-09-21T10:01:00Z", "text": text,
            "truncated": False, "sidechain": False}


def _tool(n: int) -> dict:
    return {"kind": "tool_call", "timestamp": "2026-09-21T10:01:02Z",
            "name": "Screenshot", "summary": f"shot {n}", "result": "",
            "result_truncated": False, "sidechain": False}


def _page_body(entries: list, next_cursor) -> dict:
    return {"available": True, "source": "native", "reason": None,
            "session_id": _SID, "next_cursor": next_cursor,
            "entries": entries, "tool_errors": "reported"}


def _mock_transcript(page: Page, pages: dict, calls: list) -> None:
    """``pages`` maps a ``before`` cursor (None = newest) to a page body."""
    def _handler(route):
        url = route.request.url
        if "after=" in url:
            body = {"available": True, "source": "native", "reason": None,
                    "session_id": _SID, "changed": False, "reset": False,
                    "entries": [], "pending": [], "tail": 0, "size": 1,
                    "tool_errors": "reported"}
        else:
            calls.append(url)
            m = re.search(r"[?&]before=(\d+)", url)
            body = pages[int(m.group(1)) if m else None]
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(body))

    page.route(re.compile(r".*/api/claude-code/sessions/" + _SID + r"/transcript(\?.*)?$"),
               _handler)


def _boot(page: Page, base_url: str, pages: dict) -> list:
    calls: list = []
    _mock_sessions_list(page)
    _mock_transcript(page, pages, calls)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    stub_session_mirror(page)
    open_session_row(page, page.locator(f'#sessionsList li[data-session-id="{_SID}"]'),
                     mode="chat")
    expect(page.locator("#transcriptList .tr-assistant")).to_contain_text(["newest reply"])
    return calls


def _pull(page: Page, dy: int) -> None:
    """Drag one finger ``dy`` px on the Chat list (+ down, - up), as #1292's
    pull gestures read it: a touchstart, then a touchend ``dy`` further on."""
    page.evaluate("""(dy) => {
        const box = document.getElementById('transcriptBody');
        const fire = (type, y) => {
            const ev = new Event(type, {bubbles: true});
            const pt = [{clientX: 20, clientY: y}];
            Object.defineProperty(ev, 'touches', {value: type === 'touchend' ? [] : pt});
            Object.defineProperty(ev, 'changedTouches', {value: pt});
            box.dispatchEvent(ev);
        };
        fire('touchstart', 300);
        fire('touchend', 300 + dy);
    }""", dy)


_NEWEST = _page_body([_turn("user", "newest prompt"), _tool(0), _turn("assistant", "newest reply")], 9000)


def test_one_tap_chains_past_turnless_pages_to_the_next_turn(
    authed_page: Page, base_url: str
) -> None:
    """The reported bug: the pages under the newest one hold only tool calls,
    so a single fetch added nothing visible. One tap must reach the turn."""
    page = authed_page
    calls = _boot(page, base_url, {
        None: _NEWEST,
        9000: _page_body([_tool(1), _tool(2)], 8000),
        8000: _page_body([_tool(3)], 7000),
        7000: _page_body([_turn("user", "older prompt"), _tool(4),
                          _turn("assistant", "older reply")], 6000),
        6000: _page_body([_turn("user", "oldest prompt")], None),
    })
    older = page.locator("#transcriptOlder")
    expect(older).to_be_visible()
    older.click()

    turns = page.locator("#transcriptList .tr-turn")
    expect(turns).to_have_count(4)
    expect(turns.first).to_contain_text("older prompt")
    # It stopped at the first page with a turn, not the whole file.
    assert not any("before=6000" in u for u in calls), calls
    assert [u for u in calls if "before=" in u][-1].endswith("before=7000"), calls
    # Pages render as one list: the two turn-less pages' tool calls fold into
    # one group with the tool call trailing the older reply, not one group
    # per page. Newest page's own group + those two = 3.
    expect(page.locator("#transcriptList .tr-run")).to_have_count(3)
    expect(older).to_be_visible()
    expect(older).to_have_text("Load older")
    expect(older).to_be_enabled()



@pytest.mark.iphone
def test_only_tool_calls_to_the_file_start_says_so(
    authed_page: Page, base_url: str
) -> None:
    """Reached by a pull down at the top (#1292) rather than the button: the
    path for a list too short to scroll, where no scroll event ever fires."""
    page = authed_page
    _boot(page, base_url, {
        None: _NEWEST,
        9000: _page_body([_tool(1), _tool(2)], 8000),
        8000: _page_body([_tool(3)], None),
    })
    # The list fits the pane, so no scroll event can be what loads it.
    assert page.evaluate(
        "(() => { const b = document.getElementById('transcriptBody');"
        " return b.scrollHeight <= b.clientHeight && b.scrollTop === 0; })()"
    )
    # A drag too short to be a pull loads nothing.
    _pull(page, 20)
    page.wait_for_timeout(300)
    expect(page.locator("#transcriptList .tr-start")).to_have_count(0)
    _pull(page, 120)
    expect(page.locator("#transcriptList .tr-start")).to_have_text(
        "Start of transcript — no older messages"
    )
    expect(page.locator("#transcriptList .tr-start")).to_be_visible()
    expect(page.locator("#transcriptOlder")).to_be_hidden()
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)


def test_hitting_the_chain_bound_says_what_it_loaded(
    authed_page: Page, base_url: str
) -> None:
    """Eight turn-less pages in a row stop the chain; the button reports the
    tool calls it loaded and stays tappable to keep going."""
    page = authed_page
    pages = {None: _NEWEST}
    for i in range(12):
        pages[9000 - i] = _page_body([_tool(i + 1)], 9000 - i - 1)
    pages[9000 - 12] = _page_body([_turn("user", "far prompt")], None)
    calls = _boot(page, base_url, pages)

    older = page.locator("#transcriptOlder")
    older.click()
    expect(older).to_have_text(re.compile(r"^\d+ tool calls? loaded, no messages yet — Load older$"))
    expect(older).to_be_enabled()
    # The request bound is 8; the wall-time bound can stop it sooner on a
    # loaded box. Either way the label counts exactly what was fetched.
    shown = int(older.inner_text().split()[0])
    fetched = sum(1 for u in calls if "before=" in u)
    assert 1 <= fetched <= 8 and shown == fetched, (shown, calls)
    expect(page.locator("#transcriptList .tr-turn")).to_have_count(2)

    # Keep tapping: the chain resumes from where it stopped and reaches the turn.
    for _ in range(3):
        if not older.is_visible():
            break
        older.click()
        expect(older).to_be_enabled()
    expect(page.locator("#transcriptList .tr-turn").first).to_contain_text("far prompt")
    expect(older).to_be_hidden()
