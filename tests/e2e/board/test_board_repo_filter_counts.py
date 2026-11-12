"""The Board's repo filter scopes the counts it prints, and an empty lane's action never clips.

Your turn's summary line adds up the owner checks. Under a repo filter it adds
up the ones the filter shows and says how many more sit in other projects, the
way the Chief's plan card does. Claude's turn, empty, keeps its "Start work"
action whole at every width, folded away and unfolded again, in both themes.
"""

from __future__ import annotations

import copy
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from src.board import backlog_buckets
from tests.e2e.board._support import _board_payload, _mock_board, _open_board
from tests.e2e.fixtures.waits import stable_eval

pytestmark = pytest.mark.smoke

_MINE = "demo"
_OTHER = "elsewhere"


def _issue(repo, number, labels=()):
    return {"kind": "issue", "repo": repo, "number": number, "title": f"{repo} issue {number}",
            "url": f"https://github.com/octo/{repo}/issues/{number}",
            "updated_at": "2026-07-01T10:00:00Z", "labels": list(labels)}


def _check(repo, number, kind, state="OPEN", mark=None):
    return {"ref": f"{repo}#{number}", "repo": repo, "number": number,
            "title": f"{repo} issue {number}",
            "url": f"https://github.com/octo/{repo}/issues/{number}", "state": state,
            "kind": kind, "block": True, "reason": "a real device", "try": "tap it",
            "proof": [], "since": "2026-10-30", "mark": mark, "mark_at": None, "mark_until": None}


def _payload() -> dict:
    """`demo` holds one step before start, one to validate, one to try; `elsewhere`
    holds two before start, one to validate (set aside), one to try, and one
    Waiting on you issue with no owner-check item."""
    issues = [
        _issue(_MINE, 1, ["needs-owner"]),
        _issue(_MINE, 2, ["needs-validation"]),
        _issue(_OTHER, 11, ["needs-owner"]),
        _issue(_OTHER, 12, ["needs-owner"]),
        _issue(_OTHER, 13, ["needs-validation"]),
        _issue(_OTHER, 14, ["needs-owner"]),
    ]
    items = [
        _check(_MINE, 1, "before-start"),
        _check(_MINE, 2, "validate-after"),
        _check(_MINE, 20, "validate-after", "CLOSED"),
        _check(_OTHER, 11, "before-start"),
        _check(_OTHER, 12, "before-start"),
        _check(_OTHER, 13, "validate-after", mark="problem"),
        _check(_OTHER, 21, "validate-after", "CLOSED"),
    ]
    plan = {"state": "ok", "lanes": [], "waiting": [], "queue": []}
    payload = _board_payload()
    payload["columns"]["your_turn"] = []
    payload["columns"]["claude_turn"] = []
    health = backlog_buckets.classify(
        issues, [], plan, markers_read=True, dormant_repos=set())
    health["your_turn"] = backlog_buckets.owner_counts(issues, items, plan)
    payload["columns"]["backlog"] = copy.deepcopy(issues)
    payload["backlog_buckets"] = health
    payload["owner_checks"] = {
        "available": True, "fetched_at": payload["github"]["fetched_at"], "error": None,
        "refreshing": False, "unknown": [], "items": items,
    }
    return payload


def _mock_apps(page: Page) -> None:
    page.route(
        re.compile(r".*/api/apps$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "", "apps": [
                {"id": "cc-" + name, "kind": "claude-code", "name": name,
                 "project_dir": "E:/automation/" + name}
                for name in (_MINE, _OTHER)
            ]}),
        ),
    )


def _pick(page: Page, repo: str) -> None:
    page.locator("#boardDispatchRepoInput").click()
    item = page.locator(f'#boardDispatchRepoList li[data-repo="{repo}"]')
    expect(item).to_be_visible(timeout=15_000)
    item.click()


def _assert_summary_counts_what_the_filter_shows(authed_page: Page) -> None:
    state =authed_page.locator("#boardColYours .board-owner-state")
    rows = authed_page.locator("#boardColYours .board-owner-group li.board-owner-row")
    # Fleet-wide: 3 before start + 1 validate; the problem is set aside, one issue is unlisted.
    expect(state).to_contain_text(
        "6 waiting on you (3 before you start + 1 validate to close + 1 set aside"
        " + 1 not in this list yet) · 2 to try")
    expect(state).not_to_contain_text("other projects")

    _pick(authed_page, _MINE)
    expect(rows).to_have_count(3)
    expect(state).to_contain_text(
        "2 waiting on you (1 before you start + 1 validate to close) · 1 to try")
    expect(state).to_contain_text("3 more in other projects")
    # The header's exceptions are the tab badge's, global on purpose: the filter leaves them be.
    expect(authed_page.locator("#boardHeadStatus")).to_have_text("1 failing · 6 needs you")

    _pick(authed_page, _OTHER)
    expect(rows).to_have_count(4)
    expect(state).to_contain_text(
        "4 waiting on you (2 before you start + 0 validate to close + 1 set aside + 1 not in this list yet) · 1 to try")
    expect(state).to_contain_text("3 more in other projects")

    _pick(authed_page, "")
    expect(state).not_to_contain_text("other projects")


_GEOMETRY = """col => {
  const btn = col.querySelector('.empty-state-action');
  const msg = col.querySelector('.empty-state-message');
  if (!btn || !msg) return null;
  const c = col.getBoundingClientRect(), b = btn.getBoundingClientRect();
  return {col: [c.left, c.right], btn: [b.left, b.right, b.width, b.height],
          btnClipped: btn.scrollWidth > btn.clientWidth + 0.5,
          msgVisible: msg.getBoundingClientRect().width > 0};
}"""


def _assert_start_work_whole(authed_page: Page, width: int, theme: str) -> None:
    authed_page.set_viewport_size({"width": width, "height": 900})
    authed_page.emulate_media(color_scheme=theme)
    authed_page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)

    col = authed_page.locator("#boardColClaude")
    summary = col.locator("> summary")
    if col.get_attribute("open") is None:
        summary.click()
    for folded_again in (False, True):
        if folded_again:
            summary.click()
            summary.click()
        expect(col).to_have_attribute("open", "")
        action = col.locator(".empty-state-action")
        expect(action).to_be_visible()
        expect(action).to_have_text("Start work")
        box = stable_eval(col, _GEOMETRY)
        assert box, "Claude's turn drew no empty-state action"
        assert box["col"][0] - 0.5 <= box["btn"][0] and box["btn"][1] <= box["col"][1] + 0.5, (
            f"Start work leaves the column: {box}")
        assert not box["btnClipped"], f"Start work's label is cut: {box}"
        assert box["btn"][3] >= 44 - 0.5, f"Start work is under the touch floor: {box}"


@pytest.mark.iphone
def test_repo_filter_scopes_the_counts_and_start_work_is_never_clipped(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    _mock_apps(authed_page)
    _mock_board(authed_page, _payload())
    _open_board(authed_page, base_url)
    _assert_summary_counts_what_the_filter_shows(authed_page)

    # The desktop grid needs a fine pointer; the WebKit projection is the iPhone.
    widths = (390,) if browser_name == "webkit" else (390, 1280)
    for width in widths:
        for theme in ("light", "dark"):
            _assert_start_work_whole(authed_page, width, theme)
