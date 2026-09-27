"""Regression pin for #1128: Projects, Skills and Apps rows are action-rows.

These three lists were built like spreadsheets. Life > Skills carried four
icon-only buttons per row (about 68 unlabelled targets for 17 skills),
Projects three and Apps two or three, and nothing said which one was the
primary action. The vendored action-row (project-scaffolding#268, contract
fleet-config#965) makes the row itself the primary action. It allows at most
one leading toggle (a favorite star or a tray's autostart switch) and puts
everything else behind one trailing kebab.

The rows are full-bleed (the component contract's `.card.action-list`):
edge to edge inside their card, the hairlines running the card's width,
while the card's toolbar and filter keep their inset.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read
from tests.e2e.test_row_name_typography import _APP, _PROJECT, _json_route, _mock

pytestmark = pytest.mark.smoke

# Per row: the controls a thumb can hit directly, beside the row itself.
_ROW_SHAPE = """
(sel) => Array.from(document.querySelectorAll(sel)).map((li) => ({
  id: li.dataset.id,
  main: li.querySelectorAll(':scope > .action-row-main').length,
  kebab: li.querySelectorAll(':scope > .action-row-kebab').length,
  leading: li.querySelectorAll(':scope > .action-row-fav, :scope > .toggle').length,
  direct: li.querySelectorAll(':scope > button').length,
  rail: li.querySelectorAll('.row-actions, .app-launch-actions').length,
}))
"""


# Where a list's first row sits inside its card, against the card's inner
# (padding-box) edges, plus how far the card's filter field is inset.
_ROW_BLEED = """
(sel) => {
  const li = document.querySelector(sel);
  const card = li.closest('.card');
  const c = card.getBoundingClientRect();
  const r = li.getBoundingClientRect();
  const inner = c.left + card.clientLeft;
  const filter = card.querySelector('.action-row-filter');
  return {
    left: r.left - inner,
    right: inner + card.clientWidth - r.right,
    filter: filter ? filter.getBoundingClientRect().left - inner : null,
  };
}
"""

# (tab to click, or None for the Coding tab; rows selector). One page load
# walks all four lists (#1215 merged what were three parametrize cases of
# the same test, each paying its own boot for one evaluate).
_LISTS = (
    ("#tabClaude", "#claudeList > li"),    # Code > Projects (details.projects-card)
    ("#tabLifeOS", "#lifeOsList > li"),    # Life > Skills
    ("#tabApps", "#appsList > li"),        # Apps (.apps-list-card)
    ("#tabApps", "#registeredTraysList > li"),  # Apps > Trays
)


@pytest.mark.iphone
def test_rows_are_one_primary_action_plus_one_kebab(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _mock(page)
    # The shared mock plus one tray, so the Trays list has a row to measure.
    _json_route(page, "**/api/apps", {"scan_root": "", "apps": [
        {"id": "longproj", "name": _PROJECT, "kind": "claude-code",
         "project_dir": "", "added_at": "", "is_favorite": False, "repo_url": None},
        {"id": "longapp", "name": _APP, "kind": "streamlit", "bat_path": "",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "sometray", "name": "sister-tray", "kind": "tray", "bat_path": "",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
    ]})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    for tab, rows in _LISTS:
        if tab:
            page.locator(tab).click()
        page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        expect(page.locator(rows).first).to_be_visible(timeout=5_000)

        shapes = page.evaluate(_ROW_SHAPE, rows)
        assert shapes, f"{rows}: no rows rendered"
        for s in shapes:
            assert s["main"] == 1, f"{rows} row {s['id']}: the row is not its own primary action ({s})"
            assert s["kebab"] == 1, f"{rows} row {s['id']}: expected one trailing kebab ({s})"
            assert s["leading"] <= 1, f"{rows} row {s['id']}: more than one leading toggle ({s})"
            assert s["direct"] <= 3, f"{rows} row {s['id']}: more than toggle + row + kebab ({s})"
            assert s["rail"] == 0, f"{rows} row {s['id']}: still carries an icon rail ({s})"

        # Full-bleed: the row meets both inner edges of its card, and the
        # card's filter (where it has one) keeps its inset.
        bleed = stable_read(lambda: page.evaluate(_ROW_BLEED, rows))
        assert abs(bleed["left"]) <= 1 and abs(bleed["right"]) <= 1, (
            f"{rows}: rows sit inset in their card, not full-bleed ({bleed})")
        if bleed["filter"] is not None:
            assert bleed["filter"] >= 10, f"{rows}: the filter lost its inset ({bleed})"
