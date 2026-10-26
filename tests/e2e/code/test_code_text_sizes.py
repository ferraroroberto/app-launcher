"""Pin for #1624 -- code text at the design system's two sizes (COMP-17).

The fleet rule (fleet-config#1342) measures code against ``design.md``:
inline code is the ``code-inline`` chip at 0.875em of the text around it,
never ``caption`` (12px); a listing is 13px on the ``code-listing`` frame,
and code never sits on ``canvas-subtle``, which is true black inside a dark
card. Two elements broke it, both run red against the old CSS first:

* the Channels sheet's skill-name chips were 12px (a timestamp-sized chip);
  pinned in ``test_life_tab_glance.py``, which owns that sheet's fixtures;
* the Chat transcript's tool-result block was 12px on ``--card-off``. The
  design-review walk never opens a transcript with tool results, so this
  one is pinned here, in both themes, from computed style.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS
from tests.e2e.session.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-code-sizes-1624"
_RESULT = "collected 3 items\n\ntests/test_demo.py ...   [100%]\n3 passed in 1.2s"


def _set_theme(page: Page, theme: str) -> None:
    page.emulate_media(color_scheme=theme)
    page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)


def test_tool_result_block_is_a_code_listing_in_both_themes(
    authed_page: Page, base_url: str
) -> None:
    _mock_git_status(authed_page)
    _mock_sessions_list(authed_page, [
        _session_row(_SID, kind="remote", agent="claude", title="Listing demo"),
    ])
    body = {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-10-15T09:00:00Z",
             "text": "run the demo tests", "truncated": False, "sidechain": False},
            {"kind": "tool_result", "timestamp": "2026-10-15T09:00:02Z",
             "text": _RESULT, "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-10-15T09:00:05Z",
             "text": "All three pass.", "truncated": False, "sidechain": False},
        ],
    }
    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)),
    )
    authed_page.goto(base_url, wait_until="domcontentloaded")
    _row(authed_page, _SID).locator(".session-open").click()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)

    group = authed_page.locator("#transcriptList .tr-step")
    expect(group).to_have_count(1)
    group.locator("summary.tr-step-line").click()
    item = group.locator(".tr-item").first
    item.locator("summary").click()
    pre = item.locator("pre.tr-pre")
    expect(pre).to_contain_text("3 passed in 1.2s")

    # The surface the sibling listings paint, per theme: `--card`, never the
    # inset `--card-off` (true black in dark).
    for theme in ("light", "dark"):
        _set_theme(authed_page, theme)
        expect(pre).to_have_css("font-size", "13px")
        expect(pre).to_have_css("font-family", re.compile(r"monospace"))
        expect(pre).to_have_css("border-top-width", "1px")
        expect(pre).to_have_css("border-radius", "12px")
        card = authed_page.evaluate(
            "() => { const p = document.createElement('div');"
            " p.style.background = 'var(--card)'; document.body.appendChild(p);"
            " const c = getComputedStyle(p).backgroundColor; p.remove(); return c; }")
        inset = authed_page.evaluate(
            "() => { const p = document.createElement('div');"
            " p.style.background = 'var(--card-off)'; document.body.appendChild(p);"
            " const c = getComputedStyle(p).backgroundColor; p.remove(); return c; }")
        assert card != inset, f"{theme}: --card and --card-off resolve alike; the pin would prove nothing"
        expect(pre).to_have_css("background-color", card)
        expect(pre).not_to_have_css("background-color", inset)
