"""Regression pin for #1125 — the Settings button tiers and dividers.

Save was the smallest control on the card it commands: 67x33px, set in
`button-ghost accent-btn`, a hybrid of two of design.md's four tiers ("a
tinted fill is a tint, never a ghost"). It was the view's one main action, so
it took `button-primary` — 48px, solid accent, full width. `#tokenMintBtn`,
the other user of the hybrid, takes `button-tint`, and the rule is deleted,
so no third tier can grow back.

#1435 removed Save (fields save as they change) and turned every Settings
card into a sheet; each sheet's one main action is now its footer Done, so
the primary-tier pin follows it there. The pane and the sheets ended sections
with a bare `<hr>`, the browser's own divider rather than the app's hairline.

Contrast is measured from the rendered pixels rather than assumed. Light
clears AA. **Dark does not, and cannot here**: the fleet spec sets dark
`accent: #2f81f7` with `accent-fg: #ffffff`, which is 3.75:1 — a property of
the shared token, not of this card, so the dark bar is asserted against the
spec's own value and the gap is tracked in the umbrella issue
(ferraroroberto/fleet-config#962) rather than fixed by diverging here.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import close_settings_sheets, open_settings, open_settings_sheet

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SHEETS = [
    "usageShowsSheet", "launchDefaultsSheet", "chiefSheet", "channelsSheet",
    "contextFilterSheet", "tokensSheet", "foldersSheet", "passkeysSheet",
    "terminalSheet",
]

# WCAG relative luminance / contrast, computed on the composited colours.
_CONTRAST = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el);
  const pcs = getComputedStyle(el.parentElement);
  const parse = (v) => v.match(/[\\d.]+/g).slice(0, 3).map(Number);
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((c) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const fg = lum(parse(cs.color));
  const bg = lum(parse(cs.backgroundColor));
  const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
  const r = el.getBoundingClientRect();
  return {
    ratio: Math.round(ratio * 100) / 100,
    height: Math.round(r.height),
    width: Math.round(r.width),
    // The row's content box: the sheet footer pads its button in from the
    // edge, and "spans the row" means filling what the padding leaves.
    rowWidth: Math.round(
      el.parentElement.clientWidth - parseFloat(pcs.paddingLeft) - parseFloat(pcs.paddingRight)
    ),
    background: cs.backgroundColor,
    weight: cs.fontWeight,
  };
}
"""


def _open_settings(page: Page, base_url: str, theme: str) -> None:
    page.add_init_script(f"localStorage.setItem('launcher.theme', '{theme}')")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
    open_settings(page)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_done_is_the_primary_tier(authed_page: Page, base_url: str, theme: str) -> None:
    """Each sheet's Done tier and contrast per theme, plus (merged in #1215,
    all read-only) the class-level and divider checks that used to open the
    same pane in a test of their own. Those are theme- and viewport-agnostic —
    class names, a node count, and a ``border-top-style`` from a top-level
    ``.settings-section`` rule no media query touches — so running them in
    both themes at 390px keeps every assertion as strong."""
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _open_settings(authed_page, base_url, theme)

    for sheet_id in _SHEETS:
        open_settings_sheet(authed_page, sheet_id)
        selector = f"#{sheet_id} .detail-actions .button-primary"
        m = authed_page.evaluate(_CONTRAST, selector)
        assert m is not None, f"{sheet_id}: Done button not rendered"
        assert m["height"] >= 48, (
            f"{sheet_id}: Done is {m['height']}px tall; the primary tier is 48px"
        )
        assert m["width"] >= m["rowWidth"] - 1, (
            f"{sheet_id}: Done is {m['width']}px in a {m['rowWidth']}px row — "
            "the sheet's main action spans the row"
        )
        # A solid accent fill, not a ghost's transparent one or a soft tint.
        assert m["background"] not in ("rgba(0, 0, 0, 0)", "transparent"), (sheet_id, m)
        assert int(m["weight"]) >= 700, (sheet_id, m)
        # Light clears AA; dark is the shared token's 3.75 (see the module note).
        floor = 4.5 if theme == "light" else 3.7
        assert m["ratio"] >= floor, (
            f"{sheet_id}: Done label contrast is {m['ratio']}:1 in {theme}, "
            f"under {floor}:1"
        )
        expect(authed_page.locator(selector)).to_have_class("button-primary detail-save-btn")
        if sheet_id == "passkeysSheet":
            # The passkeys sheet's status line is the one section that still
            # divides on a hairline (.webauthn-section is the sheet's first
            # block, so it has no top divider of its own any more).
            expect(authed_page.locator("#statusReadout")).to_have_css(
                "border-top-style", "solid"
            )
        close_settings_sheets(authed_page)

    # -- was test_the_ghost_accent_hybrid_is_gone --
    expect(authed_page.locator("#tokenMintBtn")).to_have_class("button-tint")
    assert authed_page.evaluate(
        "() => document.querySelectorAll('.button-ghost.accent-btn').length"
    ) == 0, "the button-ghost/accent-btn hybrid is back"

    # -- was test_settings_sections_divide_on_a_hairline --
    assert authed_page.evaluate(
        "() => document.querySelectorAll('#paneSettings hr, dialog.settings-sheet hr').length"
    ) == 0, "a bare <hr> is back in Settings"
