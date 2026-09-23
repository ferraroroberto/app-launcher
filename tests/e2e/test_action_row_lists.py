"""Regression pin for #1128: Projects, Skills and Apps rows are action-rows.

These three lists were built like spreadsheets. Life > Skills carried four
icon-only buttons per row (about 68 unlabelled targets for 17 skills),
Projects three and Apps two or three, and nothing said which one was the
primary action. The vendored action-row (project-scaffolding#268, contract
fleet-config#965) makes the row itself the primary action. It allows at most
one leading toggle (a favorite star or a tray's autostart switch) and puts
everything else behind one trailing kebab.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_row_name_typography import _mock

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


@pytest.mark.parametrize("tab, card, rows", (
    (None, "details.projects-card", "#claudeList > li"),
    ("#tabLifeOS", None, "#lifeOsList > li"),
    ("#tabApps", ".apps-list-card", "#appsList > li"),
))
def test_rows_are_one_primary_action_plus_one_kebab(
    authed_page: Page, base_url: str, tab, card, rows
) -> None:
    page = authed_page
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
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
