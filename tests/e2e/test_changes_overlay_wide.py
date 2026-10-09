"""Regression pin for #1471: on a wide window the Changed files panel is on top.

At 1100px and up (fine pointer) the Code tab's `.app` column becomes a fixed
pane beside the docked session view (#1135) at ``z-index: 310`` so the docked
view's own nested layers stay above it. The panel itself is body-level and sat
at the shared overlay layer (300), under that column, so the Coding tab's
header, Usage card, Sessions list, Projects row and build footer painted over
the left of the file list. Pinned for both ways in — a session's ⋮ → Changed
files (#1349) and a project's Show changes (#977), the same ``#changesOverlay``
— by asking ``elementFromPoint`` what is topmost over where those cards sit.

Chromium only: the wide layout is a fine-pointer query, which the WebKit
projection (an iPhone) never matches; the phone's own full-screen behaviour is
pinned by test_session_changed_files.py / test_coding_changes_overlay.py.
Every fetch is mocked before ``goto()`` (#510); data is synthetic.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_eval
from tests.e2e.test_coding_changes_overlay import AGENTS, CHANGES, _app
from tests.e2e.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = pytest.mark.smoke

_SID = "sid-changes-wide-1471"
_TRANSCRIPT = {
    "available": True, "source": "native", "reason": None, "session_id": _SID,
    "next_cursor": None, "tool_errors": "reported", "entries": [],
}
_SESSION_FILES = {
    "available": True, "reason": None, "session_id": _SID, "source": "transcript",
    "partial": False, "project_exists": True,
    "counts": {"additions": 5, "deletions": 0},
    "files": [
        {"path": "docs/notes.md", "key": "E:/work/proj/docs/notes.md", "status": "A",
         "additions": 5, "deletions": 0, "steps": 1},
    ],
}

# The probe: what is topmost at a grid of points down the panel's left column,
# the strip the Coding tab's cards (the `.app` pane beside the rail) covered.
_TOPMOST = """
(overlay => {
  const rail = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--layout-rail')) || 80;
  const r = overlay.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const x = rail + 40;
  return [24, 120, 300, 520, r.height - 40].map(y => {
    const top = document.elementFromPoint(x, y);
    return {y, onTop: !!top && overlay.contains(top),
            seen: top ? (top.id || top.className || top.tagName) : null};
  });
})
"""


def _open_session_changes(page: Page, base_url: str) -> None:
    _mock_git_status(page)
    _mock_sessions_list(page, [_session_row(_SID, kind="remote", agent="claude", title="Wide changes")])
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(_TRANSCRIPT)),
    )
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/changed-files$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(_SESSION_FILES)),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    page.locator("#terminalMenu").click()
    page.locator("#terminalOverlay .terminal-menu").get_by_role("menuitem", name="Changed files").click()


def _open_project_changes(page: Page, base_url: str) -> None:
    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "E:/automation", "apps": [_app("alpha")]}),
        ),
    )
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"agents": AGENTS, "vscode_available": True}),
        ),
    )
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": [
                {"id": "alpha", "is_git": True, "branch": "feat/977-wip", "default_branch": "main",
                 "on_default_branch": False, "dirty": True},
            ]}),
        ),
    )
    _mock_sessions_list(page, [])
    page.route(
        re.compile(r".*/api/claude-code/changes/[^/]+$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(CHANGES)),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    anchor = page.locator('.coding-item[data-id="alpha"] .project-menu-anchor')
    expect(anchor).to_be_enabled(timeout=5_000)
    anchor.click()
    page.locator('.coding-item[data-id="alpha"] .project-changes-btn').click()


@pytest.mark.parametrize("opener", [_open_session_changes, _open_project_changes],
                         ids=["session-menu", "show-changes"])
def test_changes_panel_covers_the_coding_tab_on_a_wide_window(
    authed_page: Page, base_url: str, browser_name: str, opener
) -> None:
    if browser_name != "chromium":
        pytest.skip("fine-pointer layout; the WebKit projection is an iPhone")
    page = authed_page
    page.set_viewport_size({"width": 1900, "height": 900})
    opener(page, base_url)

    overlay = page.locator("#changesOverlay")
    expect(overlay).to_be_visible()
    expect(page.locator("#changesList .chg-file").first).to_be_visible()

    probes = stable_eval(overlay, _TOPMOST)
    assert probes, "the panel has no layout box"
    covered = [p for p in probes if not p["onTop"]]
    assert not covered, f"Coding-tab layers paint over the Changed files panel: {covered}"
