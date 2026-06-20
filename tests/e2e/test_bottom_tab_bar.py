"""Regression pin for issue #267 — floating bottom tab bar polish.

Follow-up to the #263 bar (see test_primary_nav.py). Pins the three fixes on
the iPhone (WebKit) projection:

1. **No scroll drift.** The bar is promoted to its own compositing layer and a
   visualViewport pin keeps it glued to the visual viewport bottom. At a
   settled steady state that pin is a no-op, so the bar carries no residual
   translate — asserted via the computed transform being identity/none.
2. **Active pill is baseline-stable from first paint.** Every pill is a fixed
   height, centred in a fixed grid row, so the four pills share one height and
   one top edge and the active (filled) pill never protrudes above the bar's
   top edge — independent of when page content paints.
3. **Bar sits lower.** The bar's `bottom` derives from a reduced safe-area
   inset plus a 2 px gap (was 4 px), so it uses fuller vertical space while
   still clearing the home indicator.

Desktop / fine-pointer projections keep the static top control unchanged, so
the assertions are WebKit-only (matching test_primary_nav.py).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

TAB_IDS = ["#tabClaude", "#tabApps", "#tabJobs", "#tabLifeOS"]


def test_bottom_tab_bar_is_stable_and_low(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("bottom tab bar only renders on the phone projection")

    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=10_000
    )

    # Open a tab so one pill is active (filled) — the one that used to jump.
    authed_page.locator("#tabJobs").click()
    expect(authed_page.locator("#tabJobs")).to_have_attribute(
        "aria-selected", "true"
    )

    metrics = authed_page.evaluate(
        """(tabIds) => {
          const nav = document.querySelector('nav.tabs');
          const navStyle = getComputedStyle(nav);
          const navRect = nav.getBoundingClientRect();
          const pills = tabIds.map((id) => {
            const el = document.querySelector(id);
            const r = el.getBoundingClientRect();
            return {
              id,
              active: el.classList.contains('active'),
              height: Math.round(r.height),
              top: Math.round(r.top),
              bottom: Math.round(r.bottom),
            };
          });
          const t = navStyle.transform;
          const translateY = t === 'none' ? 0 : new DOMMatrix(t).m42;
          return {
            translateY: translateY,
            bottom: parseFloat(navStyle.bottom),
            navTop: Math.round(navRect.top),
            navBottom: Math.round(navRect.bottom),
            viewportHeight: window.innerHeight,
            pills,
          };
        }""",
        TAB_IDS,
    )

    # 1. Pin is a no-op at steady state — no residual vertical drift. The
    #    CSS layer-promotion uses translateZ(0), so the matrix is non-identity
    #    but its translateY component must be 0.
    assert metrics["translateY"] == 0

    # 2. All four pills share one height and one top edge (baseline-stable),
    #    and the active pill never protrudes above or below the bar.
    heights = {p["height"] for p in metrics["pills"]}
    tops = {p["top"] for p in metrics["pills"]}
    assert len(heights) == 1, f"pills differ in height: {metrics['pills']}"
    assert len(tops) == 1, f"pills off the shared baseline: {metrics['pills']}"
    active = next(p for p in metrics["pills"] if p["active"])
    assert active["top"] >= metrics["navTop"]
    assert active["bottom"] <= metrics["navBottom"]

    # 3. Bar sits low: bottom is the gap only in headless (safe-area inset 0),
    #    and the 2 px gap is tighter than the old 4 px.
    assert metrics["bottom"] <= 2
    assert metrics["navBottom"] <= metrics["viewportHeight"]
