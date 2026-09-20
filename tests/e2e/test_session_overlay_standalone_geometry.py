"""Regression pin for issue #1099 (dead band below the composer on an iPhone PWA).

``.terminal-overlay`` is the app's full-screen session shell. It was declared
``position: fixed; inset: 0`` with no installed-PWA variant, which is the one
shape ``_vendored/nav/nav-tabs.css`` explicitly names as stranding: iOS
standalone keeps the fixed-positioning viewport expanded to the full physical
screen only while the document is scrollable, and opening this overlay destroys
that twice over (``body.terminal-open { overflow: hidden }`` propagates to the
viewport; ``terminal.js``'s ``lockBodyScroll()`` takes body and the vendored 1px
``body::after`` spacer out of flow with ``position: fixed``). The viewport then
contracts from the bottom and a bottom-anchored box stops short of it. The fix
mirrors what the vendored file already does for ``.app``: anchor from the stable
top edge and size with the large viewport unit.

**What this can and cannot prove.** The band's magnitude, and that it closes, is
device-only — no headless engine reproduces iOS standalone viewport contraction,
so that criterion is verified on the phone, not here. What *is* mechanically
checkable, and is what this pin holds, is that the declaration survives:

1. a ``@media (display-mode: standalone)`` block exists whose ``.terminal-overlay``
   rule is top-anchored (``bottom: auto``) and sized with ``100lvh``;
2. the engine *accepted* ``100lvh`` — the rule ships ``height: 100vh`` first as a
   fallback, so a CSSOM read of ``100vh`` would mean this engine dropped the
   large-viewport unit and the fix silently degrades to the plain-``vh`` behaviour
   the contract replaced. This is the engine-dependent half, and the reason the
   check runs on the WebKit/iPhone projection too rather than Chromium alone;
3. the rule is *gated*: outside standalone the base ``inset: 0`` still stands, so
   ordinary browser-tab and desktop geometry is untouched.

It reads the parsed rule rather than a rendered box because that is the only read
that discriminates. With the overlay rendered, ``bottom: auto`` next to
``height: 100lvh`` resolves to the same used value (``0px``) as ``bottom: 0``, and
``height`` resolves to the viewport height either way — a desktop engine cannot
tell the two declarations apart by measurement, which is exactly why this change
is invisible to every other test in the suite.

The rule is class-wide, so it lands on all three overlays sharing the class (the
session shell plus the two Life OS full-screen shells). Those two are asserted by
*count* rather than by id on purpose: ``tests/test_classify_e2e.py``'s
surface-coverage invariant matches surface markers as text over every
``tests/e2e/test_*.py``, so naming their ids here would file this module under a
tab surface it does not exercise and route a diff for that tab into it — the same
reason ``test_chat_code_block.py`` spells its selector out in ``styles.css``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# Walk the parsed stylesheets for every `.terminal-overlay` rule sitting under a
# `display-mode: standalone` condition, at any nesting depth — the vendored nav
# file nests its own standalone block inside the coarse-pointer breakpoint, so a
# one-level scan would miss a future rule moved there.
_STANDALONE_OVERLAY_RULES_JS = r"""
() => {
  const found = [];
  const walk = (rules, conditions) => {
    for (const rule of rules || []) {
      const media = rule.media ? (rule.conditionText || '') : '';
      const next = media ? conditions.concat(media) : conditions;
      // A style rule is checked AND descended into: with CSS nesting every
      // CSSStyleRule carries an (often empty) cssRules list, so testing
      // `rule.cssRules` first would skip every selector on both engines.
      if (rule.selectorText
          && /\.terminal-overlay/.test(rule.selectorText)
          && next.some(c => /display-mode\s*:\s*standalone/.test(c))) {
        found.push({
          selector: rule.selectorText.trim(),
          conditions: next,
          top: rule.style.top,
          bottom: rule.style.bottom,
          height: rule.style.height,
        });
      }
      if (rule.cssRules && rule.cssRules.length) walk(rule.cssRules, next);
    }
  };
  for (const sheet of document.styleSheets) {
    try { walk(sheet.cssRules, []); } catch (_) { /* cross-origin sheet */ }
  }
  return found;
}
"""


def test_overlay_has_standalone_top_anchored_lvh_geometry(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay", state="attached", timeout=5_000)

    rules = authed_page.evaluate(_STANDALONE_OVERLAY_RULES_JS)
    assert rules, (
        "No `@media (display-mode: standalone)` rule for `.terminal-overlay` was "
        "parsed. Without it the overlay is bare `inset: 0` and strands above the "
        "physical bottom of an installed iOS PWA (issue #1099) — see the comment "
        "beside the `.terminal-overlay` block in styles.css."
    )
    assert len(rules) == 1, f"Expected exactly one standalone overlay rule, got {rules}"
    rule = rules[0]

    assert rule["selector"] == ".terminal-overlay", (
        "The standalone geometry must stay class-wide so every full-screen shell "
        f"carrying the class gets it, not one id: {rule['selector']!r}"
    )
    assert rule["bottom"] == "auto", (
        "The overlay must be released from the bottom edge — a bottom-anchored box "
        "follows the contracted standalone viewport, which is the band (#1099). "
        f"Got bottom: {rule['bottom']!r} in {rule['conditions']}"
    )
    assert rule["top"] in ("0px", "0"), f"Expected a stable top anchor, got {rule['top']!r}"
    assert rule["height"] == "100lvh", (
        "This engine did not accept the large viewport unit: the rule declares "
        "`height: 100vh` then `height: 100lvh`, so a CSSOM read of anything but "
        f"`100lvh` means the fallback won and the fix degrades. Got {rule['height']!r}"
    )

    # Criterion: the same geometry reaches the two Life OS full-screen shells,
    # which share the class. Counted, not named — see the module docstring.
    carriers = authed_page.evaluate(
        "() => document.querySelectorAll('.terminal-overlay').length"
    )
    assert carriers >= 3, (
        "Expected the session shell plus the two full-screen document shells to "
        f"share `.terminal-overlay`; found {carriers}."
    )

    # Gated: this run is a browser tab, not an installed PWA, so the base
    # `inset: 0` must still be what the cascade delivers. The overlay is
    # `[hidden]` (`display: none !important`), so getComputedStyle reports the
    # computed value rather than a laid-out used value — which is what makes
    # `auto` distinguishable from `0px` at all.
    overlay = authed_page.locator("#terminalOverlay")
    expect(overlay).to_have_css("bottom", "0px")
    expect(overlay).to_have_css("height", "auto")
