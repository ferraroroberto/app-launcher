"""Regression pin for #1475 (Step 2/6 of the Chat redesign, #1472): one turn,
one block, and the prompt as a bubble.

Before, every reply fragment between two tool calls was its own bordered card
with its own "Agent · time", copy and chevron, so a turn that worked in three
stretches read as three cards. Now everything the agent does until the next
prompt sits in one block under one header (the agent's mark and name, the
time, copy for the whole turn, collapse), and the prompt is a right-aligned
accent bubble at most 85% of the column (560px on a wide one).

Pinned here: one header per turn however many fragments it has, copy taking
the whole turn, collapse folding the whole turn, a tool-call-only turn hiding
with the tool calls, and the bubble's alignment and caps at 390px and 1440px
in both themes. The live-refresh merge into the open turn is pinned in
``test_chat_live_refresh.py``; the Life OS viewer mount in
``test_life_os_tab.py``.

Geometry reads go through ``stable_read`` (#680): the pane re-renders its
tail on its own poll.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._contrast import contrast_ratio
from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_read
from tests.e2e.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)
from tests.e2e.test_session_transcript import _CLIPBOARD_AND_TOAST_MOCK, _menu_item

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-turn-block-1475"


def _turn(kind: str, text: str, offset: int, ts: str) -> dict:
    return {"kind": kind, "timestamp": ts, "text": text, "truncated": False,
            "sidechain": False, "offset": offset}


def _ran(command: str, offset: int) -> dict:
    return {
        "kind": "tool_call", "timestamp": "2026-09-20T09:00:10Z", "name": "Bash",
        "summary": command, "result": "ok", "result_truncated": False,
        "sidechain": False, "offset": offset,
        "action": {"verb": "ran", "command": command},
    }


_LONG_PROMPT = "The sync badge stays Offline after the connection comes back. " * 6

_ENTRIES = [
    _turn("user", _LONG_PROMPT.strip(), 100, "2026-09-20T09:00:00Z"),
    _turn("assistant", "I'll check how the queue tracks the connection first.", 200,
          "2026-09-20T09:00:05Z"),
    _ran("grep -n online src/queue.ts", 300),
    _turn("assistant", "Found it: nothing listens for `online`.", 400, "2026-09-20T09:00:20Z"),
    _ran("npm test -- retry", 500),
    _turn("assistant", "## Fixed\n\nThe badge now goes back online.", 600,
          "2026-09-20T09:00:40Z"),
    _turn("user", "short ask", 700, "2026-09-20T09:01:00Z"),
    _ran("git status", 800),
]

# The open bubble, the list column it sits in, and the viewport width.
_BUBBLE = """
(() => {
  const turn = document.querySelector('#transcriptList .tr-user');
  const bubble = turn && turn.querySelector('div.tr-bubble');
  const list = document.getElementById('transcriptList');
  if (!bubble || !bubble.clientWidth) return null;
  const b = bubble.getBoundingClientRect(), l = list.getBoundingClientRect();
  return {left: b.left, right: b.right, width: b.width,
          listLeft: l.left, listRight: l.right, listWidth: l.width};
})()
"""


def _open(page: Page, base_url: str) -> None:
    _mock_git_status(page)
    _mock_sessions_list(page, [
        _session_row(_SID, kind="remote", agent="claude", title="Turn block demo"),
    ])
    body = {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None, "entries": _ENTRIES, "tail": 900, "size": 901,
        "tool_errors": "reported",
    }
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)),
    )
    page.add_init_script(_CLIPBOARD_AND_TOAST_MOCK)
    page.goto(base_url, wait_until="domcontentloaded")
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)


def test_a_turn_is_one_block_under_one_header(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open(page, base_url)
    listing = page.locator("#transcriptList")

    # Two prompts, two agent turns: three reply fragments make ONE block.
    expect(listing.locator(".tr-user")).to_have_count(2)
    agent = listing.locator(".tr-assistant")
    expect(agent).to_have_count(2)
    first = agent.first
    expect(first.locator(".tr-turn-summary")).to_have_count(1)
    expect(first.locator(".tr-reply")).to_have_count(3)
    expect(first.locator(".tr-turn-who")).to_have_text("Claude")
    expect(first.locator(".tr-turn-mark svg")).to_have_count(1)
    expect(first.locator(".tr-turn-summary .tr-meta")).to_have_text(re.compile(r"\d"))
    # The runs between the fragments are parts of the same block, shown as
    # step lines by default (#1476).
    expect(first.locator(".tr-run")).to_have_count(2)
    expect(first.locator(".tr-run").first).to_be_visible()

    # A turn of tool calls only shows its steps; with the steps hidden it has
    # nothing to show, so it hides with them.
    silent = listing.locator(".tr-agent-item").nth(1)
    expect(silent).to_have_class(re.compile(r"\btr-turn-silent\b"))
    expect(silent).to_be_visible()
    expect(silent.locator(".tr-turn-copy")).to_be_hidden()
    _menu_item(page, "Hide steps: tool calls and system entries").click()
    expect(silent).to_be_hidden()
    expect(first.locator(".tr-run").first).to_be_hidden()
    _menu_item(page, "Show steps: tool calls and system entries").click()
    expect(silent).to_be_visible()

    # Copy takes the whole turn, its fragments in order, one blank line apart,
    # without toggling it.
    first.locator(".tr-turn-summary .tr-turn-copy").click()
    page.wait_for_function("() => window.__copied.length > 0", timeout=3_000)
    assert page.evaluate("() => window.__copied[0]") == (
        "I'll check how the queue tracks the connection first.\n\n"
        "Found it: nothing listens for `online`.\n\n"
        "## Fixed\n\nThe badge now goes back online."
    )
    expect(first).to_have_js_property("open", True)

    # Collapse folds the whole turn to its header and first line. Closed-ness
    # is read off `open`: WebKit reports a closed details' children visible.
    first.locator(".tr-turn-summary").click()
    expect(first).to_have_js_property("open", False)
    expect(first.locator(".tr-turn-hint")).to_have_text(
        "I'll check how the queue tracks the connection first."
    )
    expect(first.locator(".tr-turn-hint")).to_be_visible()
    first.locator(".tr-turn-summary").click()
    expect(first).to_have_js_property("open", True)
    expect(first.locator(".tr-reply").nth(2)).to_be_visible()


def test_the_prompt_is_a_right_aligned_bubble(authed_page: Page, base_url: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _open(page, base_url)
    prompt = page.locator("#transcriptList .tr-user").first
    bubble = prompt.locator("div.tr-bubble")
    expect(bubble).to_be_visible()

    for width, cap in ((390, None), (1440, 560)):
        page.set_viewport_size({"width": width, "height": 900})
        m = stable_read(lambda: page.evaluate(_BUBBLE))
        assert abs(m["right"] - m["listRight"]) < 1, f"{width}px: bubble not right-aligned: {m}"
        assert m["width"] <= 0.85 * m["listWidth"] + 1, f"{width}px: bubble over 85%: {m}"
        if cap:
            assert m["width"] <= cap + 0.5, f"{width}px: bubble over {cap}px: {m}"
        assert m["left"] > m["listLeft"] + 1, f"{width}px: bubble spans the column: {m}"

    # Accent tint in both themes, text on it in fg (COLOR-02).
    for theme in ("light", "dark"):
        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        expect(bubble).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
        ratio = stable_read(lambda: contrast_ratio(bubble.locator(".tr-text")))
        assert ratio >= 4.5, f"{theme}: prompt text at {ratio:.2f}:1 on its bubble"

    # The time, copy and fold sit under the bubble; fold leaves a one-line
    # bubble that a tap opens again.
    foot = prompt.locator(".tr-prompt-foot")
    expect(foot.locator(".tr-meta")).to_have_text(re.compile(r"\d"))
    foot.locator(".tr-turn-copy").click()
    page.wait_for_function("() => window.__copied.length > 0", timeout=3_000)
    assert page.evaluate("() => window.__copied[0]") == _LONG_PROMPT.strip()
    foot.locator(".tr-prompt-fold").click()
    expect(prompt).to_have_js_property("open", False)
    folded = prompt.locator("summary.tr-prompt-folded")
    expect(folded).to_be_visible()
    folded.click()
    expect(prompt).to_have_js_property("open", True)
    expect(folded).to_be_hidden()
    expect(bubble).to_be_visible()
