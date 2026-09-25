"""Regression pin for #1127 — every rendered icon sits on the icons.size steps.

The vendored icon contract sizes a glyph at ``1em``, so an icon inside
caption or label text rendered at 12.47px or 14.72px: off the design's
16/18/20/24px steps and visibly lighter than the icons beside it. The audit
counted 12px ×9 and 15px ×7; ``design_lint`` could not see them, because it
reads only fixed ``px`` sizes. This walks every tab with every card open and
measures each rendered Lucide glyph. Brand marks (``.agent-icon``) are
excluded; they are not ``.icon`` and follow their own sizing.

The CSS-only dropdown carets are pinned too: they are Lucide chevron-down now
(a masked pseudo-element) rather than a text triangle.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.test_row_name_typography import _mock

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_STEPS = (16, 18, 20, 24)

_OFF_SCALE = """
(steps) => {
  const out = [];
  document.querySelectorAll('svg.icon').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (!r.width) return;   // not rendered (hidden tab, closed popover)
    const w = Math.round(r.width * 100) / 100;
    const h = Math.round(r.height * 100) / 100;
    if (steps.includes(w) && steps.includes(h)) return;
    const use = el.querySelector('use');
    const host = el.parentElement;
    out.push(w + 'x' + h + ' ' + (use ? use.getAttribute('href') : '?') + ' in ' +
      host.tagName.toLowerCase() + (host.id ? '#' + host.id : '') +
      (host.className && typeof host.className === 'string'
        ? '.' + host.className.trim().split(/\\s+/).join('.') : ''));
  });
  return out;
}
"""

_CARET = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el, '::after');
  return { content: cs.content, width: cs.width, mask: cs.webkitMaskImage || cs.maskImage };
}
"""


def test_rendered_icons_sit_on_the_size_steps(authed_page: Page, base_url: str) -> None:
    page = authed_page
    # Edit mode renders every row action, so more icons are measured.
    page.add_init_script("localStorage.setItem('launcher.editMode', '1')")
    _mock(page)
    page.set_viewport_size({"width": 430, "height": 932})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    strays: list[str] = []
    for tab in ("#tabClaude", "#tabApps", "#tabJobs", "#tabLifeOS", "#tabBoard", ".pane:not([hidden]) .settings-open-btn"):
        page.locator(tab).click()
        page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        page.wait_for_timeout(400)
        strays += [f"{tab}: {s}" for s in page.evaluate(_OFF_SCALE, list(_STEPS))]
    assert not strays, (
        "icons rendered off the 16/18/20/24px steps (#1127):\n  " + "\n  ".join(strays)
    )


def test_dropdown_carets_are_the_lucide_chevron(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabBoard").click()
    caret = page.evaluate(_CARET, ".board-repo-combo")
    assert caret is not None, "Board repo combo not rendered"
    assert caret["content"] in ('""', "none", "''"), (
        f"the repo dropdown caret is a text glyph ({caret['content']}), not the icon"
    )
    assert "svg" in (caret["mask"] or ""), f"caret has no chevron mask: {caret}"
    assert caret["width"] == "16px", caret
