"""Issue #953 — Coding-tab session transcript: gear menu + transcript overlay.

Pins the phone-facing contract end to end, with every non-deterministic
boot fetch stubbed **before** ``goto()`` (#510) so no poll rebuilds the row
under a click:

  * the row's single gear opens a floating menu of three actions
    (transcript · rename · stop) and any tap outside closes it; a detached
    Claude/Codex row offers the transcript too (#966), a detached row of any
    other agent does not;
  * the transcript overlay shows prompts and replies expanded, folds a run
    of tool calls / thinking into one closed disclosure, and expanding it
    (then one item) reveals the tool result;
  * "Load older" prepends the next page and hides itself once the cursor is
    exhausted;
  * an unavailable source shows its own reason line — "no transcript" and
    "read failed" are different sentences.
"""

from __future__ import annotations

import json as _json
import re

from playwright.sync_api import Page, expect

_SID = "sid-transcript-953"


def _mock_sessions_list(page: Page, *, kind: str = "pty", agent: str = "claude") -> None:
    def _handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": [{
                "session_id": _SID,
                "kind": kind,
                "agent": agent,
                "project_dir": "E:/automation/transcriptproj",
                "name": "transcriptproj",
                "alive": True,
                "started_at": "2026-09-14T10:00:00Z",
                "live_title": "",
                "prompt_title": "",
                "manual_title": "Transcript demo",
            }]}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions$"), _handler)


_NEWEST = {
    "available": True, "source": "native", "reason": None, "session_id": _SID,
    "next_cursor": 4096,
    "entries": [
        {"kind": "user", "timestamp": "2026-09-14T10:01:00Z",
         "text": "Please fix the flaky test", "truncated": False, "sidechain": False},
        {"kind": "thinking", "timestamp": "2026-09-14T10:01:01Z",
         "text": "Let me look at conftest first", "truncated": False, "sidechain": False},
        {"kind": "tool_call", "timestamp": "2026-09-14T10:01:02Z", "name": "Bash",
         "summary": "pytest tests/e2e -q", "result": "3 passed in 1.2s",
         "result_truncated": False, "sidechain": False},
        {"kind": "tool_call", "timestamp": "2026-09-14T10:01:03Z", "name": "Read",
         "summary": "tests/e2e/conftest.py", "result": "def stable_read(...)",
         "result_truncated": True, "sidechain": False},
        {"kind": "system", "timestamp": "2026-09-14T10:01:04Z", "label": "system-reminder",
         "text": "<system-reminder>ctx</system-reminder>", "truncated": False, "sidechain": False},
        {"kind": "assistant", "timestamp": "2026-09-14T10:01:05Z",
         "text": "Fixed it. The **shared budget** now applies. See https://tower.example.ts.net:8953/?token=abc.",
         "truncated": False, "sidechain": False},
    ],
}

_OLDER = {
    "available": True, "source": "native", "reason": None, "session_id": _SID,
    "next_cursor": None,
    "entries": [
        {"kind": "user", "timestamp": "2026-09-14T09:00:00Z",
         "text": "older prompt", "truncated": False, "sidechain": False},
        {"kind": "assistant", "timestamp": "2026-09-14T09:00:05Z",
         "text": "older reply", "truncated": False, "sidechain": False},
    ],
}


def _mock_transcript(page: Page, pages: dict, calls: list) -> None:
    """``pages`` maps a ``before`` cursor (None for the newest page) to the
    JSON body to serve; every request's query is recorded in ``calls``."""
    def _handler(route):
        url = route.request.url
        calls.append(url)
        m = re.search(r"[?&]before=(\d+)", url)
        before = int(m.group(1)) if m else None
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(pages[before]),
        )

    page.route(re.compile(r".*/api/claude-code/sessions/" + _SID + r"/transcript(\?.*)?$"), _handler)


def _row(page: Page):
    return page.locator(f'#sessionsList li[data-session-id="{_SID}"]')


def test_gear_menu_holds_three_actions_and_closes_on_outside_tap(
    authed_page: Page, base_url: str
) -> None:
    _mock_sessions_list(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    expect(row.locator(".name")).to_have_text("Transcript demo")

    menu = row.locator(".session-menu")
    expect(menu).to_be_hidden()
    gear = row.locator(".session-gear")
    expect(gear).to_be_visible()
    gear.click()
    expect(menu).to_be_visible()
    expect(gear).to_have_attribute("aria-expanded", "true")
    expect(menu.locator('button[aria-label="Session transcript"]')).to_be_visible()
    expect(menu.locator('button[aria-label="Rename session"]')).to_be_visible()
    expect(menu.locator('button[aria-label="Stop and kill session"]')).to_be_visible()
    assert menu.locator("button").count() == 3

    # A tap anywhere else closes it.
    authed_page.locator("#tabApps").click()
    expect(menu).to_be_hidden()


def test_detached_claude_row_opens_transcript(authed_page: Page, base_url: str) -> None:
    # #966: a detached Claude row reads the same native history a PTY row does.
    calls: list = []
    _mock_sessions_list(authed_page, kind="remote")
    _mock_transcript(authed_page, {None: _OLDER}, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    row.locator(".session-gear").click()
    menu = row.locator(".session-menu")
    expect(menu).to_be_visible()
    expect(menu.locator("button")).to_have_count(3)
    menu.locator('button[aria-label="Session transcript"]').click()
    expect(authed_page.locator("#transcriptOverlay")).to_be_visible()
    expect(authed_page.locator("#transcriptList .tr-user").first).to_contain_text("older prompt")


def test_detached_unsupported_agent_menu_has_no_transcript(authed_page: Page, base_url: str) -> None:
    _mock_sessions_list(authed_page, kind="remote", agent="pi")
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    row.locator(".session-gear").click()
    menu = row.locator(".session-menu")
    expect(menu).to_be_visible()
    expect(menu.locator("button")).to_have_count(2)
    expect(menu.locator('button[aria-label="Session transcript"]')).to_have_count(0)


def test_transcript_shows_turns_folds_tools_and_loads_older(
    authed_page: Page, base_url: str
) -> None:
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, {None: _NEWEST, 4096: _OLDER}, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    row.locator(".session-gear").click()
    row.locator('button[aria-label="Session transcript"]').click()

    overlay = authed_page.locator("#transcriptOverlay")
    expect(overlay).to_be_visible()
    expect(authed_page.locator("#transcriptTitle")).to_have_text("Transcript demo")
    # The list-row menu closed behind the overlay.
    expect(row.locator(".session-menu")).to_be_hidden()

    turns_user = authed_page.locator("#transcriptList .tr-user")
    turns_agent = authed_page.locator("#transcriptList .tr-assistant")
    expect(turns_user).to_have_count(1)
    expect(turns_user.first).to_contain_text("Please fix the flaky test")
    expect(turns_agent).to_have_count(1)
    expect(turns_agent.first).to_contain_text("Fixed it.")
    # Markdown rendered, not shown raw.
    expect(turns_agent.first.locator("strong")).to_have_text("shared budget")
    # A bare URL in the reply is a real link (trailing period not swallowed).
    link = turns_agent.first.locator("a")
    expect(link).to_have_attribute("href", "https://tower.example.ts.net:8953/?token=abc")
    expect(link).to_have_attribute("target", "_blank")

    # Turns are collapsible cards, open by default; the bar's toggle closes
    # every turn (and reopens them) while the tool group stays closed.
    expect(turns_user.first).to_have_js_property("open", True)
    expect(turns_agent.first).to_have_js_property("open", True)
    toggle = authed_page.locator("#transcriptToggleAll")
    expect(toggle).to_have_attribute("aria-label", "Collapse all turns")
    toggle.click()
    expect(turns_user.first).to_have_js_property("open", False)
    expect(turns_agent.first).to_have_js_property("open", False)
    expect(authed_page.locator("#transcriptList .tr-group")).to_have_js_property("open", False)
    expect(toggle).to_have_attribute("aria-label", "Expand all turns")
    toggle.click()
    expect(turns_agent.first).to_have_js_property("open", True)
    # A single turn collapses on its own summary.
    turns_user.first.locator("summary").click()
    expect(turns_user.first).to_have_js_property("open", False)
    turns_user.first.locator("summary").click()
    expect(turns_user.first).to_have_js_property("open", True)

    # The four non-conversation entries fold into one closed group — hidden
    # by default (the eye toggle, between reload and collapse-all, shows it).
    group = authed_page.locator("#transcriptList .tr-group")
    expect(group).to_have_count(1)
    expect(group).to_be_hidden()
    eye = authed_page.locator("#transcriptToggleGroups")
    expect(eye).to_have_attribute("aria-label", "Show tool calls and system entries")
    bar_ids = authed_page.locator("#transcriptOverlay .terminal-bar-actions button").evaluate_all(
        "els => els.map(e => e.id)"
    )
    assert bar_ids == ["transcriptRefresh", "transcriptToggleGroups", "transcriptToggleAll", "transcriptClose"], bar_ids
    eye.click()
    expect(group).to_be_visible()
    expect(eye).to_have_attribute("aria-label", "Hide tool calls and system entries")
    expect(group.locator(".collapse-title")).to_have_text("2 tool calls · 1 thinking · 1 system")
    items = group.locator(".tr-item")
    expect(items).to_have_count(4)
    # Closed-ness is asserted on the <details> `open` property, not on child
    # visibility: WebKit reports a closed details' children as visible.
    expect(group).to_have_js_property("open", False)
    group.locator("summary.collapse-summary").click()
    expect(group).to_have_js_property("open", True)
    expect(items.first).to_be_visible()
    # Each item is still closed until tapped; the second one is the Bash call.
    bash = items.nth(1)
    expect(bash.locator(".tr-item-name")).to_have_text("Bash")
    expect(bash.locator(".tr-pre")).to_have_count(2)
    expect(bash).to_have_js_property("open", False)
    bash.locator("summary").click()
    expect(bash).to_have_js_property("open", True)
    expect(bash.locator(".tr-pre").last).to_be_visible()
    expect(bash.locator(".tr-pre").last).to_contain_text("3 passed in 1.2s")
    # The Read result was capped server-side and says so.
    read = items.nth(2)
    read.locator("summary").click()
    expect(read.locator(".tr-trunc")).to_contain_text("(truncated)")

    # Load older: the previous page prepends, the button goes away at the end.
    older = authed_page.locator("#transcriptOlder")
    expect(older).to_be_visible()
    older.click()
    expect(turns_user).to_have_count(2)
    expect(turns_user.first).to_contain_text("older prompt")
    expect(turns_user.last).to_contain_text("Please fix the flaky test")
    expect(older).to_be_hidden()
    assert any("before=4096" in url for url in calls), calls

    # ✕ (always the rightmost bar control) returns to the list, overlay gone.
    bar_buttons = authed_page.locator("#transcriptOverlay .terminal-bar-actions button")
    expect(bar_buttons.last).to_have_attribute("id", "transcriptClose")
    authed_page.locator("#transcriptClose").click()
    expect(overlay).to_be_hidden()


def test_unavailable_reasons_are_distinct_sentences(authed_page: Page, base_url: str) -> None:
    calls: list = []
    _mock_sessions_list(authed_page)
    unavailable = {"available": False, "source": None, "reason": "no_transcript",
                   "entries": [], "next_cursor": None, "session_id": _SID}
    _mock_transcript(authed_page, {None: unavailable}, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    row.locator(".session-gear").click()
    row.locator('button[aria-label="Session transcript"]').click()
    state = authed_page.locator("#transcriptState")
    expect(state).to_be_visible()
    expect(state).to_have_text("No transcript found for this session")
    expect(authed_page.locator("#transcriptOlder")).to_be_hidden()

    # Same overlay, a read failure: a different sentence, never "nothing".
    unavailable["reason"] = "read_failed"
    authed_page.locator("#transcriptRefresh").click()
    expect(state).to_have_text("Couldn’t read the transcript")
