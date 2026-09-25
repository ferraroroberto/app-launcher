"""Regression pin for #1141 — a GFM table in rendered markdown.

``markdown.js`` had no table rule, so an agent's table fell through to a
paragraph: header, delimiter and body rows joined with spaces into one ``<p>``
of pipes and dashes, which on a phone is close to unreadable. The renderer is
shared, so this pins both kinds of consumer:

* the Coding tab's **Chat pane** (``.tr-md``, the same renderer the Life OS
  conversation viewer reuses), and
* the Life OS **document viewer** (``#lifeOsFileContent``).

Each leg asserts a real ``<table>`` with header, body and column alignment,
inline markdown in cells with HTML still escaped, and the geometry the issue
asks for: a wide table scrolls sideways inside its own wrapper at phone width
while the page never gains a horizontal scroll. The parser's edge cases (pipes
in code spans and fences, a lone ``a | b`` line, ragged rows) are pinned
without a browser in ``tests/js/markdown.test.mjs``.

Content is synthetic. Style assertions use auto-retrying ``expect()`` and the
raw geometry read goes through ``stable_read`` (#680) — the chat transcript
re-renders on its own poll.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_read
from tests.e2e.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-md-table-1141"

# Six columns of unbreakable tokens: wider than any phone, so the table has
# to scroll inside its wrapper rather than squeeze or widen the page.
_TABLE = "\n".join([
    "| Module | Status | Tests | p50 ms | Owner | Note |",
    "|:--|:-:|--:|--:|---|---|",
    "| `session_transcript_renderer` | **green** | 128 | 4.25 | platform_team | <b>raw</b> |",
    "| scheduler_dispatch_queue | amber | 7 | 312.00 | [runbook](https://example.com/runbook) | stable_after_retry |",
])
_REPLY = (
    "Here is the comparison:\n\n" + _TABLE + "\n\n"
    "Plain pipes stay prose: a | b"
)

# The wrapper's scroll geometry plus whether the document itself gained a
# horizontal scroll. `scope` is the consumer's container selector.
_MEASURE = """
(scope) => {
  const wrap = document.querySelector(scope + ' .md-table-wrap');
  // Null (missing, or zero-width mid-render) is the artifact `stable_read`
  // retries past.
  if (!wrap || !wrap.clientWidth) return null;
  const de = document.documentElement;
  return {
    scrollWidth: wrap.scrollWidth,
    clientWidth: wrap.clientWidth,
    right: Math.round(wrap.getBoundingClientRect().right),
    docScroll: de.scrollWidth - de.clientWidth,
  };
}
"""


def _assert_table(page: Page, scope: str) -> None:
    table = page.locator(f"{scope} .md-table-wrap > table.md-table")
    expect(table).to_be_visible()
    expect(table.locator("thead th")).to_have_count(6)
    expect(table.locator("tbody tr")).to_have_count(2)
    expect(table.locator("thead th").nth(0)).to_have_text("Module")

    # Alignment from the delimiter row's colons.
    expect(table.locator("thead th").nth(0)).to_have_css("text-align", "left")
    expect(table.locator("thead th").nth(1)).to_have_css("text-align", "center")
    expect(table.locator("tbody tr").first.locator("td").nth(2)).to_have_css("text-align", "right")

    # Inline markdown inside cells, and HTML in a cell stays text.
    first = table.locator("tbody tr").first
    expect(first.locator("td").nth(0).locator("code")).to_have_text("session_transcript_renderer")
    expect(first.locator("td").nth(1).locator("strong")).to_have_text("green")
    expect(table.locator("tbody tr").nth(1).locator("td").nth(4).locator("a")).to_have_attribute(
        "href", "https://example.com/runbook")
    expect(first.locator("td").nth(5)).to_have_text("<b>raw</b>")
    expect(first.locator("td b")).to_have_count(0)

    # Wide content scrolls sideways inside the wrapper; the page never does.
    expect(page.locator(f"{scope} .md-table-wrap")).to_have_css("overflow-x", "auto")
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_timeout(150)
        m = stable_read(lambda: page.evaluate(_MEASURE, scope))
        assert m is not None, f"no table wrapper found in {scope} at {width}px"
        assert m["scrollWidth"] > m["clientWidth"], (
            f"at {width}px the six-column table fits in {m['clientWidth']}px — "
            "it is being squeezed instead of scrolling inside its wrapper"
        )
        assert m["right"] <= width, (
            f"at {width}px the table wrapper's right edge is at {m['right']}px, "
            "outside the viewport"
        )
        assert m["docScroll"] <= 0, (
            f"at {width}px the table gave the page {m['docScroll']}px of "
            "horizontal scroll"
        )


def _mock_transcript(page: Page) -> None:
    body = {
        "available": True, "source": "native", "reason": None, "session_id": _SID,
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-09-22T09:00:00Z",
             "text": "compare the modules", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-09-22T09:00:05Z",
             "text": _REPLY, "truncated": False, "sidechain": False},
        ],
    }
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)),
    )


def test_chat_renders_a_markdown_table(authed_page: Page, base_url: str) -> None:
    _mock_git_status(authed_page)
    _mock_sessions_list(authed_page, [
        _session_row(_SID, kind="remote", agent="claude", title="Table demo"),
    ])
    _mock_transcript(authed_page)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    _row(authed_page, _SID).locator(".session-open").click()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)

    _assert_table(authed_page, "#transcriptList .tr-md")
    # A lone pipe in prose is not a table.
    expect(authed_page.locator("#transcriptList .tr-md p").last).to_have_text(
        "Plain pipes stay prose: a | b")
    expect(authed_page.locator("#transcriptList .tr-md table")).to_have_count(1)


def _mock_life_os_doc(page: Page) -> None:
    def _json_route(pattern: str, body: dict) -> None:
        page.route(
            re.compile(pattern),
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=_json.dumps(body)),
        )

    _json_route(r".*/api/life-os/recap-status$", {
        "available": False, "ledger_exists": False, "age_days": None,
        "staleness": "fresh", "proposal_pending": False, "proposal_name": None,
    })
    _json_route(r".*/api/life-os/skills(\?.*)?$", {
        "available": True, "life_os_dir": "",
        "skills": [{
            "id": "demo-skill", "name": "demo-skill", "command": "demo-skill",
            "description": "Synthetic skill.", "skill_md": ".claude/skills/demo-skill/SKILL.md",
        }],
    })
    _json_route(r".*/api/life-os/skills/demo-skill/files$", {
        "skill": {"id": "demo-skill", "name": "demo-skill"},
        "files": [{"path": ".claude/skills/demo-skill/SKILL.md",
                   "name": "SKILL.md", "category": "skill"}],
    })
    _json_route(r".*/api/life-os/file\?.*$", {
        "path": "x", "name": "SKILL.md", "truncated": False,
        "content": "# Demo\n\n" + _TABLE + "\n",
    })


def test_life_os_document_renders_a_markdown_table(authed_page: Page, base_url: str) -> None:
    _mock_life_os_doc(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()
    tile = authed_page.locator("#lifeOsList li.lifeos-item[data-id='demo-skill']")
    expect(tile).to_be_visible()
    # Read lives in the row's ⋯ menu since #1128.
    tile.locator(".action-row-kebab").click()
    tile.locator("button[title^='Browse']").click()
    authed_page.locator(".lifeos-file-btn").first.click()
    expect(authed_page.locator("#lifeOsFileContent")).to_be_visible()

    _assert_table(authed_page, "#lifeOsFileContent")
