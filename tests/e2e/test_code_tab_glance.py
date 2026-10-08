"""The Code tab's quieter Sessions list and header (issue #1434, step 2/7 of #1432).

The Code rows speak the Board's status vocabulary without polling the Board:
``/api/claude-code/sessions`` carries each session's Board column and status
(``board_column``, ``board_status``), and a session the Board files under
Your turn shows an attention "needs you" chip, a stalled one a danger
"stalled" chip. An unknown placement (``null``) shows no chip: unknown is
never turned into "needs you". The page header follows the header rule:
only the exceptions, each in its tone, else the plain session count, and
never the running-apps or git counts.

Hermetic: the session list is route-mocked before ``goto()`` (#510), with
fictional sessions.
"""

from __future__ import annotations

import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_eval

pytestmark = pytest.mark.smoke

_NOW = int(time.time())


def _session(sid: str, column, status, **extra) -> dict:
    s = {"session_id": sid, "kind": "pty", "agent": "claude", "label": "",
         "project_dir": "E:/work/" + sid, "name": sid, "flags": "",
         "started_at": _NOW - 600, "alive": True, "rows": 40, "cols": 120,
         "live_title": "", "prompt_title": sid + " title", "manual_title": "",
         "output_chars": 10, "board_column": column, "board_status": status}
    s.update(extra)
    return s


_CHIEF = _session("s-chief", "claude_turn", "awaiting-input", label="chief",
                  name="chief", manual_title="chief")
_WAITING = _session("s-wait", "your_turn", "awaiting-decision")
_STALLED = _session("s-stall", "your_turn", "stalled")
_WORKING = _session("s-work", "claude_turn", "working")
_UNKNOWN = _session("s-unknown", None, None)


def _mock(page: Page, sessions: list[dict]) -> None:
    def json_route(pattern: str, body: dict) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/claude-code/sessions$", {"sessions": sessions})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    json_route(r".*/api/rate-limits$", {"quota_lines": []})


def _row(page: Page, sid: str):
    return page.locator(f'#sessionsList li[data-session-id="{sid}"]')


def test_rows_carry_the_boards_needs_you_and_stalled_chips(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page, [_CHIEF, _WAITING, _STALLED, _WORKING, _UNKNOWN])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    waiting = _row(authed_page, "s-wait").locator(".session-attention")
    expect(waiting).to_have_text("needs you")
    expect(waiting).to_have_attribute("data-tone", "attention")
    stalled = _row(authed_page, "s-stall").locator(".session-attention")
    expect(stalled).to_have_text("stalled")
    expect(stalled).to_have_attribute("data-tone", "danger")

    # A normal state gets no chip, and neither does an unknown one.
    for sid in ("s-chief", "s-work", "s-unknown"):
        expect(_row(authed_page, sid).locator(".session-attention")).to_have_count(0)

    # The header names the exceptions in their tones, nothing else.
    head = authed_page.locator("#homeHeadStatus")
    expect(head).to_have_text("1 needs you · 1 stalled")
    parts = head.locator(".head-exception")
    expect(parts).to_have_count(2)
    expect(parts.nth(0)).to_have_attribute("data-tone", "attention")
    expect(parts.nth(1)).to_have_attribute("data-tone", "danger")


def test_header_falls_back_to_the_plain_count(
    authed_page: Page, base_url: str
) -> None:
    """Nothing waits on the user (one row's placement unknown): the plain
    count, with no running-apps or git part (#1434)."""
    _mock(authed_page, [_CHIEF, _WORKING, _UNKNOWN])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    head = authed_page.locator("#homeHeadStatus")
    expect(head).to_have_text("3 sessions")
    expect(head.locator(".head-exception")).to_have_count(0)
    expect(head).not_to_contain_text("running")
    expect(head).not_to_contain_text("dirty")


@pytest.mark.iphone
def test_header_exceptions_fit_at_390px(authed_page: Page, base_url: str) -> None:
    """The header rule's two parts fit the phone's header line uncut."""
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _mock(authed_page, [_CHIEF, _WAITING, _STALLED])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    head = authed_page.locator("#homeHeadStatus")
    expect(head).to_have_text("1 needs you · 1 stalled")
    widths = stable_eval(
        head, "el => el.isConnected && el.clientWidth ? [el.clientWidth, el.scrollWidth] : null"
    )
    assert widths[1] <= widths[0], f"the header line is cut at 390px: {widths}"


@pytest.mark.iphone
def test_stopped_chief_row_fits_at_390px(authed_page: Page, base_url: str) -> None:
    """With no chief running, its row's title and meta stay uncut beside the
    Start and Resume verbs on a phone (#1434)."""
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _mock(authed_page, [_WORKING])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    row = authed_page.locator("#sessionsList .chief-stopped-row")
    expect(row.locator(".srow-title")).to_have_text("Chief")
    expect(row.locator(".srow-meta-text")).to_have_text("stopped")
    cut = stable_eval(row, """el => {
      if (!el.isConnected) return null;
      const parts = [el.querySelector('.srow-title'), el.querySelector('.srow-meta-text')];
      if (parts.some(p => !p || !p.clientWidth)) return null;
      return parts.filter(p => p.scrollWidth > p.clientWidth).map(p => p.textContent);
    }""")
    assert cut == [], f"the stopped chief row cuts {cut} at 390px"
