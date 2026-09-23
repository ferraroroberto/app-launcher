"""Regression pin for #1124 — the 44px touch floor, measured as rendered.

The UX audit measured large shares of controls under the fleet's 44px floor
on the iPhone projection: 77% of the Apps tab's, 43% of the Jobs tab's, 52%
of Settings'. `design_lint`'s static `hit-target` check passed throughout,
because the *authored* width is 44 while the *rendered* height was 33-39px —
which is the whole reason this is an e2e pin and not a lint rule.

Geometry comes from `tests/e2e/_geometry.py`, project-scaffolding's helper
(#157) vendored verbatim: it measures the effective rectangle, visual box
plus any `::before`/`::after` negative-inset expansion, and asserts the floor
and pairwise non-overlap. Two controls may each reach 44px invisibly, but if
their expanded rectangles intersect, a tap in the shared zone is ambiguous —
so both halves are asserted.

The sweep has no exclusions for sub-floor controls. The old `.segmented`
control (29px buttons) was left out of the first version on purpose, to keep
that gap visible. It is now the vendored `range-tab` (#1133), whose `::before`
expands every pill to 44px vertically without overlapping its neighbours, so
the sweep covers it like everything else.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._geometry import assert_min_target, assert_no_overlap
from tests.e2e.test_row_name_typography import _mock

pytestmark = pytest.mark.smoke

# Everything a thumb can hit.
_CONTROLS = (
    "button, select, input[type='number'], "
    "input[type='text'], [role='switch'], [role='tab'], summary"
)

# Every control's effective rectangle, for the sweep below. Mirrors
# _geometry.py's own JS so one page.evaluate covers a whole tab (a per-locator
# walk over ~200 controls is minutes of round-trips).
_SWEEP = """
(sel) => {
  const out = [];
  document.querySelectorAll(sel).forEach((el) => {
    // The floating nav is excluded: it is the vendored nav component's own
    // contract (53x53 at rest, measured), and it animates on boot and hides
    // under an overlay -- so a sweep that opens every <details> catches it
    // mid-transition at a fraction of its real box and reports a defect
    // that is not there.
    if (el.closest('.tabs')) return;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;   // not rendered on this tab
    const exp = { l: 0, r: 0, t: 0, b: 0 };
    for (const which of ['::before', '::after']) {
      const ps = getComputedStyle(el, which);
      if (ps.content === 'none' || ps.position !== 'absolute') continue;
      for (const [k, side] of [['l','left'],['r','right'],['t','top'],['b','bottom']]) {
        const v = parseFloat(ps[side]);
        if (Number.isFinite(v) && v < 0) exp[k] = Math.max(exp[k], -v);
      }
    }
    out.push({
      w: r.width + exp.l + exp.r,
      h: r.height + exp.t + exp.b,
      left: r.left - exp.l, right: r.right + exp.r,
      top: r.top - exp.t, bottom: r.bottom + exp.b,
      what: (el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
        : el.tagName.toLowerCase())
        + ' "' + (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24) + '"',
    });
  });
  return out;
}
"""

_TABS = ("#tabClaude", "#tabApps", "#tabJobs", "#tabLifeOS", "#tabBoard", "#tabSettings")


@pytest.mark.parametrize("tab", _TABS)
def test_every_control_meets_the_44px_floor(
    authed_page: Page, base_url: str, tab: str
) -> None:
    page = authed_page
    page.add_init_script("localStorage.setItem('launcher.editMode', '1')")
    _mock(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator(tab).click()
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    page.wait_for_timeout(400)

    rects = page.evaluate(_SWEEP, _CONTROLS)
    assert rects, f"{tab}: no controls measured — wrong selector?"
    # 43.99, not 44: a 36px control grown by 2x4 lands on 43.999… in WebKit's
    # fractional layout, which is the floor met, not missed.
    under = [
        f"{r['what']} {r['w']:.1f}x{r['h']:.1f}"
        for r in rects if r["w"] < 43.99 or r["h"] < 43.99
    ]
    assert not under, (
        f"{tab}: {len(under)} control(s) under the 44px effective floor "
        "(#1124):\n  " + "\n  ".join(sorted(set(under)))
    )


@pytest.mark.parametrize("cluster", (
    ".sessions-header-actions",
    ".compose-tools",
    ".board-dispatch-row",
    ".app-item .app-launch-actions",
))
def test_expanded_targets_in_a_cluster_do_not_overlap(
    authed_page: Page, base_url: str, cluster: str
) -> None:
    """Invisible expansion is only legitimate where it cannot collide: two
    controls whose grown rectangles intersect share tappable pixels."""
    page = authed_page
    page.add_init_script("localStorage.setItem('launcher.editMode', '1')")
    _mock(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    if cluster == ".board-dispatch-row":
        page.locator("#tabBoard").click()
    elif cluster == ".app-item .app-launch-actions":
        page.locator("#tabApps").click()
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    page.wait_for_timeout(400)

    # `:visible` matters: a cluster holds controls that are hidden until
    # something opens them (a row menu, a mode-specific tool), and a hidden
    # element measures as a zero box at the origin — every one of which
    # "overlaps" every other.
    buttons = page.locator(f"{cluster} button:visible")
    if buttons.count() < 2:
        pytest.skip(f"{cluster} renders fewer than two controls here")
    assert_no_overlap(buttons)


def test_the_helper_is_wired_to_a_real_locator(
    authed_page: Page, base_url: str
) -> None:
    """One assertion through `_geometry.assert_min_target` itself, so the
    vendored helper is exercised rather than only copied in."""
    page = authed_page
    _mock(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    launch = page.locator("#appsList .app-launch-actions button").first
    expect(launch).to_be_visible()
    assert_min_target(launch)
