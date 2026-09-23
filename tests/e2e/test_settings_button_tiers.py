"""Regression pin for #1125 — the Settings card's button tiers and dividers.

Save was the smallest control on the card it commands: 67x33px, set in
`button-ghost accent-btn`, a hybrid of two of design.md's four tiers ("a
tinted fill is a tint, never a ghost"). It is the view's one main action, so
it takes `button-primary` — 48px, solid accent, full width. `#tokenMintBtn`,
the other user of the hybrid, takes `button-tint`, and the rule is deleted,
so no third tier can grow back.

The same card ended two of its sections with a bare `<hr>`, the browser's own
divider rather than the app's hairline.

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

pytestmark = pytest.mark.smoke

# WCAG relative luminance / contrast, computed on the composited colours.
_CONTRAST = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el);
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
    rowWidth: Math.round(el.parentElement.getBoundingClientRect().width),
    background: cs.backgroundColor,
    weight: cs.fontWeight,
  };
}
"""


def _open_settings(page: Page, base_url: str, theme: str) -> None:
    page.add_init_script(f"localStorage.setItem('launcher.theme', '{theme}')")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
    page.locator(".pane:not([hidden]) .settings-open-btn").click()
    page.locator("#settingsPanel").evaluate("el => { el.open = true; }")
    expect(page.locator("#saveSettings")).to_be_visible()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_save_is_the_primary_tier(authed_page: Page, base_url: str, theme: str) -> None:
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _open_settings(authed_page, base_url, theme)

    m = authed_page.evaluate(_CONTRAST, "#saveSettings")
    assert m is not None, "Save button not rendered"
    assert m["height"] >= 48, f"Save is {m['height']}px tall; the primary tier is 48px"
    assert m["width"] >= m["rowWidth"] - 1, (
        f"Save is {m['width']}px in a {m['rowWidth']}px row — the view's main "
        "action spans the row"
    )
    # A solid accent fill, not a ghost's transparent one or a soft tint.
    assert m["background"] not in ("rgba(0, 0, 0, 0)", "transparent"), m
    assert int(m["weight"]) >= 700, m
    # Light clears AA; dark is the shared token's 3.75 (see the module note).
    floor = 4.5 if theme == "light" else 3.7
    assert m["ratio"] >= floor, (
        f"Save label contrast is {m['ratio']}:1 in {theme}, under {floor}:1"
    )


def test_the_ghost_accent_hybrid_is_gone(authed_page: Page, base_url: str) -> None:
    _open_settings(authed_page, base_url, "light")
    expect(authed_page.locator("#saveSettings")).to_have_class("button-primary")
    expect(authed_page.locator("#tokenMintBtn")).to_have_class("button-tint")
    assert authed_page.evaluate(
        "() => document.querySelectorAll('.button-ghost.accent-btn').length"
    ) == 0, "the button-ghost/accent-btn hybrid is back"


def test_settings_sections_divide_on_a_hairline(authed_page: Page, base_url: str) -> None:
    _open_settings(authed_page, base_url, "light")
    assert authed_page.evaluate(
        "() => document.querySelectorAll('#paneSettings hr').length"
    ) == 0, "a bare <hr> is back in the Settings pane"
    for selector in ("#paneSettings .webauthn-section", "#statusReadout"):
        expect(authed_page.locator(selector)).to_have_css("border-top-style", "solid")
