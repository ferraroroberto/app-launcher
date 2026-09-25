"""Issue #953 — Coding-tab session transcript: gear menu + Chat mode.

Pins the phone-facing contract end to end, with every non-deterministic
boot fetch stubbed **before** ``goto()`` (#510) so no poll rebuilds the row
under a click. Since #982 the transcript is the **Chat mode** of the one
session overlay (``#terminalOverlay[data-mode="chat"]``), opened from the
gear's "Open chat" item; the pane's own ids (``#transcriptList`` …) are
unchanged, and the bar's 👁 / 🔄 became the ⋮ menu's chat-only "Show tool
calls" / "Reload transcript" (the ⇕ collapse-all was dropped — a turn still
collapses on its own summary):

  * the row's single gear opens a floating menu (Terminal · Chat · Rename ·
    Stop for a full-control row) and any tap outside closes it; a detached
    Claude/Codex row offers Chat too (#966), a detached row of any other
    agent does not;
  * Chat mode shows prompts and replies expanded, folds a run of tool calls /
    thinking into one closed disclosure, and expanding it (then one item)
    reveals the tool result;
  * a failed tool call is marked — a red glyph and a "failed" chip on the
    row, a "1 failed" chip on the *closed* group — while a harness that
    cannot report failures says so in the expanded card instead of letting
    silence read as success (#1020);
  * "Load older" prepends the next page and hides itself once the cursor is
    exhausted;
  * an unavailable source shows its own reason line — "no transcript" and
    "read failed" are different sentences.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._contrast import contrast_ratio
from tests.e2e.conftest import (
    OVERLAY_OPEN_MS,
    open_session_row,
    stable_read,
    stub_session_mirror,
)

pytestmark = pytest.mark.smoke

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


def _open_chat(page: Page, row) -> None:
    # #1025: the row's gear is gone; Chat is the row tap plus the bar's Chat
    # segment. The mirror stub keeps a full-control row's tap in-page on the
    # Chromium projection (#282).
    stub_session_mirror(page)
    open_session_row(page, row, mode="chat")


def _menu_item(page: Page, name: str):
    page.locator("#terminalMenu").click()
    menu = page.locator("#terminalOverlay .terminal-menu")
    expect(menu).to_be_visible()
    return menu.get_by_role("menuitem", name=name)


# The row's own action menu (#1025). `name` is the item's accessible name,
# which row-menu.js sets from each item's `label`.
def _row_menu_item(page: Page, row, name: str):
    row.locator(".session-kebab").click()
    menu = row.locator(".session-menu")
    expect(menu).to_be_visible()
    return menu.get_by_role("menuitem", name=name)


def test_row_carries_a_centred_kebab_and_no_chevron(
    authed_page: Page, base_url: str
) -> None:
    """#1025 — the row's anchor is a kebab in the slot the gear held,
    centred against the whole row, and no chevron is rendered.

    The first attempt at this issue shipped the inverse (gear removed,
    chevron kept), which took the row's entire action set with it. This pins
    the corrected shape so neither half can regress alone."""
    _mock_sessions_list(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    expect(row.locator(".name")).to_have_text("Transcript demo")

    # Neither the old gear glyph nor the chevron survives.
    expect(row.locator(".session-gear")).to_have_count(0)
    expect(row.locator(".session-chevron")).to_have_count(0)

    kebab = row.locator(".session-kebab")
    expect(kebab).to_be_visible()
    # Centred against the whole row — the alignment half of #1025, and the
    # one part the first attempt got right. Both boxes are read through
    # `stable_read` because the Coding rows are rebuilt by the git-status
    # poll (#680), and compared with a 1px tolerance for sub-pixel heights.
    row_box = stable_read(row.bounding_box)
    keb_box = stable_read(kebab.bounding_box)
    row_mid = row_box["y"] + row_box["height"] / 2
    keb_mid = keb_box["y"] + keb_box["height"] / 2
    assert abs(row_mid - keb_mid) <= 1, (
        f"kebab centre {keb_mid} is not the row centre {row_mid}"
    )
    # Pinned to the row's right edge, in the rail the gear occupied.
    right_gap = (row_box["x"] + row_box["width"]) - (keb_box["x"] + keb_box["width"])
    assert 0 <= right_gap <= 1, f"kebab is not flush right (gap {right_gap})"
    # The row's own tap target stays a full-size row.
    open_box = stable_read(row.locator(".session-open").bounding_box)
    assert open_box["height"] >= 44, open_box["height"]


def test_row_menu_holds_the_full_option_set_in_order(
    authed_page: Page, base_url: str
) -> None:
    """#1025 — the kebab's menu carries exactly the options the gear did.

    The regression this pins is the menu silently emptying: the first attempt
    deleted every one of these and nothing failed, because no test asserted
    the row's own menu contents. Order and accessible names are both part of
    the contract."""
    _mock_sessions_list(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    row.locator(".session-kebab").click()
    menu = row.locator(".session-menu")
    expect(menu).to_be_visible()

    # A full-control Claude row offers all four, top to bottom.
    items = menu.get_by_role("menuitem")
    expect(items).to_have_count(4)
    assert [
        (items.nth(i).get_attribute("aria-label") or "").strip()
        for i in range(4)
    ] == [
        "Open terminal",
        "Open chat",
        "Rename session",
        "Stop and kill session",
    ]
    # Stop keeps the class that paints it danger-red on press.
    expect(menu.locator(".action-stop-close")).to_have_count(1)


def test_row_tap_opens_the_session_and_kebab_tap_does_not(
    authed_page: Page, base_url: str
) -> None:
    """#1025 — the kebab lives outside the row button, so it opens the menu
    and never the session; the row itself still opens the session."""
    _mock_sessions_list(authed_page)
    stub_session_mirror(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    overlay = authed_page.locator("#terminalOverlay")

    # Kebab: menu opens, overlay stays shut.
    row.locator(".session-kebab").click()
    expect(row.locator(".session-menu")).to_be_visible()
    expect(overlay).to_be_hidden()

    # Close the menu (tap the anchor again), then tap the row itself.
    row.locator(".session-kebab").click()
    expect(row.locator(".session-menu")).to_be_hidden()
    row.locator(".session-open").click()
    authed_page.wait_for_selector(
        "#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS
    )


def test_detached_claude_row_opens_chat(authed_page: Page, base_url: str) -> None:
    # #966: a detached Claude row reads the same native history a PTY row does.
    calls: list = []
    _mock_sessions_list(authed_page, kind="remote")
    _mock_transcript(authed_page, {None: _OLDER}, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    # A detached row has no terminal, so its tap lands straight in Chat — no
    # mirror stub needed, and since #1025 no gear to go through.
    open_session_row(authed_page, row)
    overlay = authed_page.locator("#terminalOverlay")
    expect(overlay).to_have_attribute("data-mode", "chat")
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-disabled", "true")
    expect(authed_page.locator("#transcriptList .tr-user").first).to_contain_text("older prompt")


def test_detached_reader_less_row_still_reaches_rename_and_stop(
    authed_page: Page, base_url: str
) -> None:
    """#1025's condition, pinned: the one row shape that can offer *neither*
    pane must still be renameable and stoppable.

    `ssh` is the example on purpose — it is the one registered agent that
    will never have a harness history (`SESSION_HOST_AGENTS`, #558), so this
    stays true however many coding agents gain readers. Detached + no reader
    used to render the row **inert**, with its gear as the only way to Rename
    or Stop it; #1025 removed the gear, so instead of stranding the row the
    inert shape went too. The row now opens the overlay on the reader's own
    reason line, and Rename / Stop live in the bar's ⋮ menu.
    """
    calls: list = []
    _mock_sessions_list(authed_page, kind="remote", agent="ssh")
    _mock_transcript(authed_page, {None: {
        "available": False, "source": None, "reason": "unsupported_agent",
        "entries": [], "next_cursor": None, "session_id": _SID,
    }}, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)

    # No inert shape left, and no gear to be the only way in.
    expect(row.locator(".session-open.inert")).to_have_count(0)
    expect(row.locator(".session-gear")).to_have_count(0)

    open_session_row(authed_page, row)
    overlay = authed_page.locator("#terminalOverlay")
    # Neither pane is on offer, and the view says why rather than sitting blank.
    expect(overlay).to_have_attribute("data-mode", "chat")
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-disabled", "true")
    expect(authed_page.locator("#sessionModeChat")).to_have_attribute("aria-disabled", "true")
    expect(authed_page.locator("#transcriptState")).to_contain_text(
        "Transcript not supported for this agent yet"
    )

    # Both actions the gear used to hold are reachable here. One open of the
    # ⋮ menu, two reads — `_menu_item` toggles the anchor, so calling it
    # twice would close the menu again.
    authed_page.locator("#terminalMenu").click()
    menu = authed_page.locator("#terminalOverlay .terminal-menu")
    expect(menu).to_be_visible()
    expect(menu.locator('button[aria-label="Rename session"]')).to_be_visible()
    expect(menu.locator('button[aria-label="Stop and kill session"]')).to_be_visible()
    # /compact is Claude Code's command: a non-Claude agent gets no Compact
    # item (#1218), where it would be sent as a prompt.
    expect(menu.locator('button[aria-label="Compact conversation"]')).to_have_count(0)


def test_transcript_shows_turns_folds_tools_and_loads_older(
    authed_page: Page, base_url: str
) -> None:
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(authed_page, {None: _NEWEST, 4096: _OLDER}, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    _open_chat(authed_page, row)

    overlay = authed_page.locator("#terminalOverlay")
    expect(authed_page.locator("#terminalTitle")).to_have_text("Transcript demo")
    # The row's own menu exists (#1025) but stays closed behind the overlay:
    # opening a session is not what opens it.
    expect(row.locator(".session-menu")).to_be_hidden()
    # The shared bar: the context ring's slot (#1223), then the Terminal /
    # Chat segments, 🔊 and ⋮ after them, ⋮ last (#981/#982); no chat-only
    # bar buttons. The ring stays hidden here: this session's context use is
    # unknown (the host has no such session), and unknown is never drawn.
    bar_ids = authed_page.locator("#terminalOverlay .terminal-bar-actions button").evaluate_all(
        "els => els.map(e => e.id)"
    )
    assert bar_ids == [
        "contextRing", "sessionModeTerminal", "sessionModeChat", "terminalSpeak", "terminalMenu",
    ], bar_ids
    expect(authed_page.locator("#contextRing")).to_be_hidden()
    expect(authed_page.locator("#sessionModeChat")).to_have_attribute("aria-pressed", "true")
    # A full-control session has no detached note.
    expect(authed_page.locator("#chatNote")).to_be_hidden()

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

    # Turns are collapsible cards, open by default; a single turn collapses
    # on its own summary while the tool group stays closed.
    expect(turns_user.first).to_have_js_property("open", True)
    expect(turns_agent.first).to_have_js_property("open", True)
    turns_user.first.locator("summary").click()
    expect(turns_user.first).to_have_js_property("open", False)
    expect(authed_page.locator("#transcriptList .tr-group")).to_have_js_property("open", False)
    turns_user.first.locator("summary").click()
    expect(turns_user.first).to_have_js_property("open", True)

    # The four non-conversation entries fold into one closed group — hidden
    # by default; the ⋮ menu's chat-only "Show tool calls" shows it, and the
    # item's label flips once they show.
    group = authed_page.locator("#transcriptList .tr-group")
    expect(group).to_have_count(1)
    expect(group).to_be_hidden()
    menu = authed_page.locator("#terminalOverlay .terminal-menu")
    eye = _menu_item(authed_page, "Show tool calls and system entries")
    expect(menu.locator(".row-menu-label")).to_have_text(
        ["Rename", "Copy link", "Show tool calls", "Reload", "Compact", "Stop and kill"]
    )
    eye.click()
    expect(menu).to_be_hidden()
    expect(group).to_be_visible()
    expect(_menu_item(authed_page, "Hide tool calls and system entries")).to_be_visible()
    authed_page.locator("#terminalMenu").click()  # close it again
    expect(menu).to_be_hidden()
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

    # ‹ Back returns to the list, overlay gone.
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()


def test_unavailable_reasons_are_distinct_sentences(authed_page: Page, base_url: str) -> None:
    calls: list = []
    _mock_sessions_list(authed_page)
    unavailable = {"available": False, "source": None, "reason": "no_transcript",
                   "entries": [], "next_cursor": None, "session_id": _SID}
    _mock_transcript(authed_page, {None: unavailable}, calls)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = _row(authed_page)
    _open_chat(authed_page, row)
    state = authed_page.locator("#transcriptState")
    expect(state).to_be_visible()
    expect(state).to_have_text("No transcript found for this session")
    expect(authed_page.locator("#transcriptOlder")).to_be_hidden()

    # Same pane, a read failure after ⋮ → Reload: a different sentence, never
    # "nothing".
    unavailable["reason"] = "read_failed"
    _menu_item(authed_page, "Reload transcript").click()
    expect(state).to_have_text("Couldn’t read the transcript")


# Capture writeText payloads on window.__copied, same mock as
# test_jobs_log_copy.py (#97): headless WebKit clipboard permissions are not
# reliable, and this mock is what makes the copy check meaningful in *both*
# projections instead of skipping the one closest to the phone (#985). Also
# records every toast shown on window.__toasts via a MutationObserver — the
# truncated path shows two toasts in a row fast enough (both mocked calls
# resolve within a tick or two) that reading the live `.toast` element would
# race whichever one is current at assertion time; the recorded history
# doesn't.
_CLIPBOARD_AND_TOAST_MOCK = """
(() => {
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (t) => { window.__copied.push(t); },
      readText: async () => '',
    },
  });
  window.__toasts = [];
  const watch = () => {
    const el = document.getElementById('toast');
    if (!el) { requestAnimationFrame(watch); return; }
    new MutationObserver(() => {
      if (!el.hidden) window.__toasts.push(el.textContent.trim());
    }).observe(el, { attributes: true, childList: true, characterData: true, subtree: true });
  };
  watch();
})()
"""


def test_copy_button_copies_turn_text_and_upgrades_a_truncated_reply(
    authed_page: Page, base_url: str
) -> None:
    """#985: the copy glyph on a user card copies its text outright; on an
    assistant card whose entry came back ``truncated: true`` it writes the
    capped text immediately (the iOS-safe synchronous write), then fetches
    ``/transcript/entry`` and *visibly* upgrades the clipboard — a second
    toast, never a silent rewrite."""
    calls: list = []
    _mock_sessions_list(authed_page)
    full_reply = "R" * 20_000
    capped_reply = full_reply[:12_000]
    page_body = {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-09-14T10:01:00Z", "offset": 0,
             "text": "short prompt", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-09-14T10:01:05Z", "offset": 4096,
             "text": capped_reply, "truncated": True, "sidechain": False},
        ],
    }
    _mock_transcript(authed_page, {None: page_body}, calls)

    def _entry_handler(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "available": True, "reason": None, "session_id": _SID,
                "text": full_reply, "truncated": False,
            }),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/transcript/entry(\?.*)?$"),
        _entry_handler,
    )
    authed_page.add_init_script(_CLIPBOARD_AND_TOAST_MOCK)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = _row(authed_page)
    _open_chat(authed_page, row)

    # The user card: a plain, un-truncated copy — one write, one toast, and
    # the card stays open (the tap must not also toggle the <details>).
    user_turn = authed_page.locator("#transcriptList .tr-user").first
    expect(user_turn).to_have_js_property("open", True)
    user_turn.locator(".tr-turn-copy").click()
    authed_page.wait_for_function(
        "() => Array.isArray(window.__copied) && window.__copied.length > 0", timeout=3_000,
    )
    assert authed_page.evaluate("() => window.__copied[0]") == "short prompt"
    expect(authed_page.locator(".toast")).to_contain_text("Prompt copied")
    expect(user_turn).to_have_js_property("open", True)

    # The assistant card: truncated, so the capped text lands first, then the
    # full entry fetch upgrades it — both writes, and both toasts along the
    # way, read back from the recorded history rather than the live DOM.
    assistant_turn = authed_page.locator("#transcriptList .tr-assistant").first
    assistant_turn.locator(".tr-turn-copy").click()
    authed_page.wait_for_function(
        "() => Array.isArray(window.__copied) && window.__copied.length > 2", timeout=3_000,
    )
    assert authed_page.evaluate("() => window.__copied[1]") == capped_reply
    assert authed_page.evaluate("() => window.__copied[2]") == full_reply
    toasts = authed_page.evaluate("() => window.__toasts")
    assert any("loading the full text" in t for t in toasts), toasts
    assert any("Full reply copied" in t for t in toasts), toasts
    assert toasts.index(next(t for t in toasts if "loading the full text" in t)) < \
        toasts.index(next(t for t in toasts if "Full reply copied" in t)), toasts


# ------------------------------------------------------- #1020 tool errors

def _outcome_page(*, tool_errors: str, failed: bool) -> dict:
    """One page holding a working call and, optionally, a failed one."""
    entries = [
        {"kind": "user", "timestamp": "2026-09-14T10:01:00Z",
         "text": "read both files", "truncated": False, "sidechain": False},
        {"kind": "tool_call", "timestamp": "2026-09-14T10:01:02Z", "name": "Read",
         "summary": "docs/board.md", "result": "# Board",
         "result_truncated": False, "sidechain": False},
    ]
    if failed:
        entries.append(
            {"kind": "tool_call", "timestamp": "2026-09-14T10:01:03Z", "name": "Read",
             "summary": "docs/gone.md", "result": "File does not exist.",
             "result_truncated": False, "sidechain": False, "error": True},
        )
    entries.append(
        {"kind": "assistant", "timestamp": "2026-09-14T10:01:05Z",
         "text": "done", "truncated": False, "sidechain": False},
    )
    return {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None, "tool_errors": tool_errors, "entries": entries,
    }


def _open_tool_group(page: Page):
    """Chat open, tool calls revealed, the group still closed."""
    row = _row(page)
    _open_chat(page, row)
    expect(page.locator("#transcriptList .tr-turn")).not_to_have_count(0)
    _menu_item(page, "Show tool calls and system entries").click()
    group = page.locator("#transcriptList .tr-group")
    expect(group).to_be_visible()
    return group


def test_failed_tool_call_is_marked_in_both_themes(
    authed_page: Page, base_url: str
) -> None:
    """#1020 — a failed call read exactly like a working one.

    The marker has to survive the two things that made the old behaviour
    invisible: the group is *closed* by default (so the count rides on the
    closed header), and the row's hint ellipses at phone width (so the chip
    is its own non-shrinking element, not more hint text). Asserted in both
    themes because ``--danger`` is redefined for dark.
    """
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(
        authed_page,
        {None: _outcome_page(tool_errors="reported", failed=True)},
        calls,
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    group = _open_tool_group(authed_page)

    # Legible while still folded: the count rides on the closed header.
    expect(group).to_have_js_property("open", False)
    expect(group.locator(".tr-fail-count")).to_have_text("1 failed")

    group.locator("summary.collapse-summary").click()
    expect(group).to_have_js_property("open", True)
    items = group.locator(".tr-item")
    expect(items).to_have_count(2)

    # The working call carries no marker of any kind — a success is not
    # decorated, only a failure is.
    ok, bad = items.nth(0), items.nth(1)
    expect(ok).not_to_have_class(re.compile(r"tr-item-failed"))
    expect(ok.locator(".tr-fail-chip")).to_have_count(0)
    expect(bad).to_have_class(re.compile(r"tr-item-failed"))
    expect(bad.locator(".tr-fail-chip")).to_have_text("failed")

    # A row-level mark, not a banner: the turn cards around it are untouched.
    expect(authed_page.locator("#transcriptList .tr-turn.tr-item-failed")).to_have_count(0)

    # Both themes resolve --danger to a real colour, and the two differ —
    # `to_have_css` re-resolves the locator, so a re-render can't yield the
    # '' WebKit returns from a raw getComputedStyle read (#680).
    seen = {}
    user_meta = authed_page.locator("#transcriptList .tr-user .tr-meta")
    expect(user_meta.first).to_be_visible()
    for theme in ("light", "dark"):
        authed_page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        chip = bad.locator(".tr-fail-chip")
        expect(chip).not_to_have_css("color", "rgba(0, 0, 0, 0)")
        seen[theme] = chip.evaluate("el => getComputedStyle(el).color")
        # The user turn's "You · time" line on its accent tint (#1238,
        # COLOR-02): it measured 4.17:1 light / 4.21:1 dark in muted text.
        ratio = stable_read(lambda: contrast_ratio(user_meta))
        assert ratio >= 4.5, f"{theme}: user-turn meta at {ratio:.2f}:1, under 4.5:1"
    assert seen["light"] and seen["dark"], seen
    assert seen["light"] != seen["dark"], seen

    # Folded calls stay folded: marking one never opens it.
    expect(bad).to_have_js_property("open", False)


def test_unreported_outcome_says_so_instead_of_reading_as_success(
    authed_page: Page, base_url: str
) -> None:
    """#1020's harder half — Codex records no outcome at all.

    Nothing may be marked (there is nothing to mark), but the silence must
    not pass for success either: the expanded card says the outcome was
    never recorded. The note is in the *body*, so a folded group stays as
    quiet as it was before.
    """
    calls: list = []
    _mock_sessions_list(authed_page, agent="codex")
    _mock_transcript(
        authed_page,
        {None: _outcome_page(tool_errors="none", failed=False)},
        calls,
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    group = _open_tool_group(authed_page)

    # Nothing claimed, nothing decorated, nothing counted.
    expect(group.locator(".tr-fail-count")).to_have_count(0)
    expect(group.locator(".tr-item-failed")).to_have_count(0)
    # …and the closed header is unchanged from a `reported` flavour.
    expect(group.locator(".collapse-title")).to_have_text("1 tool call")

    group.locator("summary.collapse-summary").click()
    item = group.locator(".tr-item").first
    item.locator("summary").click()
    expect(item).to_have_js_property("open", True)
    note = item.locator(".tr-outcome-unknown")
    expect(note).to_be_visible()
    expect(note).to_contain_text("doesn\u2019t record whether a tool call failed")
    # Muted, not a status colour — "nobody can tell" is not a failure, and
    # dressing it as one would be the same lie in reverse. Compared against
    # the row hint, the other --muted text on the same card, so this needs
    # no colour arithmetic to hold in both themes.
    hint_color = item.locator(".tr-item-hint").evaluate(
        "el => getComputedStyle(el).color"
    )
    expect(note).to_have_css("color", hint_color)


def test_reported_harness_adds_no_note_to_a_clean_call(
    authed_page: Page, base_url: str
) -> None:
    """The third state exists only where it is true: a harness that reports
    every failure leaves an unmarked call to mean "it worked", with no
    caveat line cluttering every card."""
    calls: list = []
    _mock_sessions_list(authed_page)
    _mock_transcript(
        authed_page,
        {None: _outcome_page(tool_errors="reported", failed=False)},
        calls,
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    group = _open_tool_group(authed_page)
    group.locator("summary.collapse-summary").click()
    item = group.locator(".tr-item").first
    item.locator("summary").click()
    expect(item).to_have_js_property("open", True)
    expect(item.locator(".tr-pre")).not_to_have_count(0)
    expect(item.locator(".tr-outcome-unknown")).to_have_count(0)
