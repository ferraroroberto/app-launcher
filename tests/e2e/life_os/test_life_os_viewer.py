"""Life OS tab e2e: a conversation opened in the chat transcript viewer."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.terminal.test_overlay_standalone_scrollable import (
    _PATCH_MATCH_MEDIA,
    _PROJECT_STANDALONE_CSS,
)

from tests.e2e.life_os._support import (
    _FAKE_TRANSCRIPT,
    _close_sheet,
    _convo_rows,
    _mock_conversations,
    _mock_skills,
    _mock_transcript,
    _open_conversations,
    _open_sheet,
    _open_viewer,
    _skill_menu_item,
)


pytestmark = pytest.mark.smoke


# A capture carrying an AskUserQuestion call (#1149). Today's capture reader
# drops tool calls upstream, so this pins the renderer's own guarantee: a
# question card mounted here is history, never a control.
_QUESTION_TRANSCRIPT = dict(_FAKE_TRANSCRIPT, entries=_FAKE_TRANSCRIPT["entries"] + [
    {"kind": "tool_call", "name": "AskUserQuestion", "summary": "questions",
     "call_id": "toolu_viewer", "result": None, "result_truncated": False,
     "sidechain": False, "offset": 300, "timestamp": None,
     "questions": [{"question": "Which sailing?", "header": "Ferry",
                    "multiSelect": False, "options": [
                        {"label": "07:40", "description": "first boat"},
                        {"label": "11:15", "description": "late morning"}]}]},
    {"kind": "tool_call", "name": "ExitPlanMode", "summary": "# Book it",
     "call_id": "toolu_viewer_plan", "plan": "## Book it\n\n- pay by card",
     "plan_truncated": False, "result": "User has approved your plan.",
     "plan_outcome": "approved", "result_truncated": False,
     "sidechain": False, "offset": 400, "timestamp": None},
])


def test_capture_opens_in_the_chat_transcript_view(
    authed_page: Page, base_url: str
) -> None:
    """#1119: Read opens the capture in the session overlay's own Chat
    renderer — real turn cards, the user's prompt as plain text and the reply
    through markdown — not the raw document viewer. One surface, two mounts
    (#979), so the assertion is on that renderer's own classes. #1149: a
    question card renders here too, and read-only. #1504: the reader is a
    sheet over the conversation sheet, which keeps Open raw."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, _QUESTION_TRANSCRIPT)
    authed_page.route(re.compile(r".*/api/life-os/file\?.*$"), lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(
            {"path": "x", "name": "2026-08-01-0900-ferry-booking.md",
             "content": "You: book the ferry", "truncated": False})))
    _open_conversations(authed_page, base_url)

    viewer_list = _open_viewer(authed_page, 0)
    expect(authed_page.locator("#lifeOsViewerTitle")).to_have_text("booking the ferry")
    turns = viewer_list.locator(".tr-turn")
    expect(turns).to_have_count(2)
    expect(turns.first).to_have_class(re.compile(r"\btr-user\b"))
    expect(turns.first).to_contain_text("book the ferry for Friday")
    expect(turns.nth(1)).to_have_class(re.compile(r"\btr-assistant\b"))
    # The reply goes through the markdown renderer; the prompt does not.
    expect(turns.nth(1).locator(".tr-md strong")).to_have_text("07:40")
    # Read-only: no composer anywhere in the reader.
    expect(authed_page.locator("#lifeOsConvoViewer .composer")).to_have_count(0)
    # #1149: the question renders as its card, beside the turns rather than
    # in a group, with no live control — not one enabled option, no text
    # field — and says it holds no answer instead of offering one.
    card = viewer_list.locator(".tr-ask-item")
    expect(card).to_be_visible()
    expect(card.locator(".tr-ask-question")).to_have_text("Which sailing?")
    expect(card.locator(".tr-ask-label")).to_have_text(["07:40", "11:15"])
    expect(card.locator(".tr-ask-desc").first).to_have_text("first boat")
    expect(card).to_have_attribute("data-mode", "history")
    expect(card.locator(".tr-ask-opt:enabled")).to_have_count(0)
    expect(card.locator(".tr-ask-input")).to_be_hidden()
    expect(card.locator(".tr-ask-status")).to_have_text("No answer recorded here")
    # #1151: a plan renders as its card here too — markdown, its outcome,
    # and nothing to tap.
    plan = viewer_list.locator(".tr-plan-item")
    expect(plan.locator(".tr-plan-body h2")).to_have_text("Book it")
    expect(plan).to_have_attribute("data-mode", "approved")
    expect(plan.locator("button")).to_have_count(0)
    # Done comes back to the sheet; its Open raw stacks the raw document over
    # it, and the document's ✕ comes back to the sheet again.
    authed_page.locator("#lifeOsViewerDone").click()
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_hidden()
    sheet = authed_page.locator("#lifeOsConvoSheet")
    expect(sheet).to_be_visible()
    sheet.locator(".lifeos-convo-open-raw").click()
    expect(authed_page.locator("#lifeOsDoc")).to_be_visible()
    expect(authed_page.locator("#lifeOsFileContent")).to_contain_text("book the ferry")
    authed_page.locator("#lifeOsDocClose").click()
    expect(authed_page.locator("#lifeOsDoc")).to_be_hidden()
    expect(sheet).to_be_visible()


_LONG_TRANSCRIPT = dict(_FAKE_TRANSCRIPT, entries=[
    {"kind": "user" if i % 2 == 0 else "assistant",
     "text": f"synthetic turn {i} " + "about ferry timetables and ticket prices " * 3,
     "offset": i * 100, "timestamp": None, "truncated": False}
    for i in range(40)
])


def test_viewer_latest_pill_jumps_to_the_last_turn(
    authed_page: Page, base_url: str
) -> None:
    """#1140: the shared ↓ Latest pill on the conversation reader. A capture
    that fits has no end to jump to. A long one still opens at its start — it
    reads top-down — but shows the pill straight away, and one tap reaches
    the last turn."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    # The first row's capture is long, the second's two turns.
    authed_page.route(re.compile(r".*/api/life-os/file/transcript.*"), lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(
            _LONG_TRANSCRIPT if "ferry-booking" in route.request.url else _FAKE_TRANSCRIPT)))
    _open_conversations(authed_page, base_url)

    pill = authed_page.locator("#lifeOsViewerLatest")
    expect(_open_viewer(authed_page, 1).locator(".tr-turn")).to_have_count(2)
    expect(pill).to_be_hidden()
    authed_page.locator("#lifeOsViewerClose").click()
    _close_sheet(authed_page)

    turns = _open_viewer(authed_page, 0).locator(".tr-turn")
    expect(turns).to_have_count(40)
    expect(turns.first).to_be_in_viewport()
    expect(pill).to_be_visible()

    pill.click()
    expect(turns.last).to_be_in_viewport()
    expect(pill).to_be_hidden()


_MEASURE_PILL = """
() => {
  const pill = document.getElementById('lifeOsViewerLatest');
  const r = pill.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {
    vh: window.innerHeight, vw: window.innerWidth,
    top: r.top, bottom: r.bottom, left: r.left, right: r.right,
    hittable: !!hit && (hit === pill || pill.contains(hit)),
    hitBy: hit ? (hit.id || hit.className || hit.tagName) : null,
    appOverflowY: getComputedStyle(document.querySelector('main.app')).overflowY,
  };
}
"""


@pytest.mark.iphone
def test_reader_covers_the_tab_bar_in_the_standalone_shell(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#1143: in the installed PWA the floating tab bar sat on top of the
    conversation viewer and hid its ↓ Latest pill. Since #1504 the reader and
    the conversation sheet are native dialogs in the top layer, which the
    vendored shell hides the bar for (body:has(dialog[open])): the pill must
    be on screen and hittable, and .app locked behind them.

    Neither headless engine supports display-mode: standalone, so this
    projects the shell the way #1099's test does. A pass here doesn't replace
    checking on the device."""
    page = authed_page
    if browser_name == "chromium":
        # The phone rules also need a coarse pointer.
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    page.add_init_script(_PATCH_MATCH_MEDIA)
    page.set_viewport_size({"width": 390, "height": 844})
    _mock_skills(page)
    _mock_conversations(page)
    _mock_transcript(page, _LONG_TRANSCRIPT)
    page.goto(f"{base_url}/", wait_until="load")
    assert page.evaluate(_PROJECT_STANDALONE_CSS) >= 1, (
        "no display-mode: standalone rule to project; the vendored shell "
        "block moved and this would test the browser-tab layout instead"
    )
    tabs = page.locator("nav.tabs")
    expect(tabs).to_be_visible()

    page.locator("#tabLifeOS").click()
    _skill_menu_item(page, "journal-daily", "[data-item=lifeos-convo-btn]").click()
    expect(_convo_rows(page)).to_have_count(2, timeout=5_000)
    _open_sheet(page, 0)
    expect(tabs).to_be_hidden()

    expect(_open_viewer_from_sheet(page).locator(".tr-turn")).to_have_count(40)
    expect(tabs).to_be_hidden()
    pill = page.locator("#lifeOsViewerLatest")
    expect(pill).to_be_visible()
    m = page.evaluate(_MEASURE_PILL)
    assert m["top"] >= 0 and m["bottom"] <= m["vh"] + 0.5, f"pill off-screen vertically: {m}"
    assert m["left"] >= 0 and m["right"] <= m["vw"] + 0.5, f"pill off-screen horizontally: {m}"
    assert m["hittable"], f"the ↓ Latest pill is covered at its centre by {m['hitBy']!r}: {m}"
    assert m["appOverflowY"] == "hidden", f".app stays scrollable behind the reader: {m}"
    pill.click()
    expect(page.locator("#lifeOsViewerList .tr-turn").last).to_be_in_viewport()

    page.locator("#lifeOsViewerClose").click()
    _close_sheet(page)
    expect(tabs).to_be_visible()


def _open_viewer_from_sheet(page: Page) -> Locator:
    page.locator("#lifeOsConvoSheet .lifeos-convo-read").click()
    expect(page.locator("#lifeOsConvoViewer")).to_be_visible(timeout=5_000)
    return page.locator("#lifeOsViewerList")


def test_unparseable_capture_falls_back_to_the_raw_view(
    authed_page: Page, base_url: str
) -> None:
    """#1119: a capture the parser can make nothing of says so in one line and
    offers the raw file — never a blank pane, and never an empty conversation
    passed off as a read one."""
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    _mock_transcript(authed_page, {
        "path": _FAKE_TRANSCRIPT["path"], "name": _FAKE_TRANSCRIPT["name"],
        "available": False, "reason": "no_turns", "agent": "", "entries": [],
    })
    _open_conversations(authed_page, base_url)

    viewer_list = _open_viewer(authed_page, 0)
    expect(viewer_list.locator(".tr-turn")).to_have_count(0)
    state = authed_page.locator("#lifeOsViewerState")
    expect(state).to_be_visible()
    expect(state).to_contain_text("no turns this view can read")
    # Passive status belongs beside the surface, not in a toast.
    expect(authed_page.locator("#toast")).to_be_hidden()
    state.locator(".lifeos-viewer-raw").click()
    expect(authed_page.locator("#lifeOsDoc")).to_be_visible()
