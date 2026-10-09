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

Since #1435 every Settings card is a modal sheet, and a closed dialog renders
nothing, so a sweep that only reads the pane would measure none of them (a
silent coverage loss). The Settings case opens each sheet in turn - and each
agent's sheet - and sweeps the controls it shows.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._geometry import assert_min_target, assert_no_overlap
from tests.e2e.conftest import (
    close_settings_sheets,
    open_agent_sheet,
    open_settings_sheet,
)
from tests.e2e.test_row_name_typography import _json_route, _mock

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

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

_SETTINGS = ".pane:not([hidden]) .settings-open-btn"
_TABS = ("#tabClaude", "#tabApps", "#tabJobs", "#tabLifeOS", "#tabBoard", _SETTINGS)

# Every Settings sheet, and the agents whose own sheet opens from Launch
# defaults (#1435). A sheet's data-rendered control, where it has one, is
# waited for before its sweep.
_SHEETS = (
    "usageShowsSheet", "launchDefaultsSheet", "chiefSheet", "channelsSheet",
    "contextFilterSheet", "tokensSheet", "foldersSheet", "passkeysSheet",
    "terminalSheet",
)
_AGENTS = ("claude", "codex", "antigravity", "copilot", "pi", "grok")
_SHEET_DATA_CONTROL = {"passkeysSheet": "#webauthnDevices .icon-button"}

# A control a tab renders only once its data arrives, which the sweep waits
# for rather than racing the boot fetch. (The Jobs agenda's Add job action
# is no longer on the tab: since #1438 it sits in the agenda sheet, swept
# separately below.)
_DATA_CONTROL = {
    "#tabLifeOS": "#lifeOsRecapLaunch",
}


def _assert_helper_is_wired_to_a_real_locator(page: Page) -> None:
    """One assertion through `_geometry.assert_min_target` itself, so the
    vendored helper is exercised rather than only copied in.

    Formerly `test_the_helper_is_wired_to_a_real_locator`; folded into the
    `#tabApps` sweep below (#1215), which loads the same mocked Apps tab at
    the same 390x844 viewport with every <details> open."""
    launch = page.locator("#appsList .action-row-kebab").first
    expect(launch).to_be_visible()
    assert_min_target(launch)


def _assert_cluster_does_not_overlap(page: Page, cluster: str) -> None:
    """Invisible expansion is only legitimate where it cannot collide: two
    controls whose grown rectangles intersect share tappable pixels.

    Formerly `test_expanded_targets_in_a_cluster_do_not_overlap`, one
    parametrize case per cluster; each case now runs inside the floor sweep
    of the tab that renders it (#1215). Its `.sessions-header-actions` and
    `.compose-tools` cases were dropped there: both always skipped, the
    first holding a single button (the old Git status button) and the second having no
    visible composer on the Code tab, so neither ever asserted anything."""
    # `:visible` matters: a cluster holds controls that are hidden until
    # something opens them (a row menu, a mode-specific tool), and a hidden
    # element measures as a zero box at the origin — every one of which
    # "overlaps" every other.
    buttons = page.locator(f"{cluster} button:visible")
    assert buttons.count() >= 2, f"{cluster} renders fewer than two controls here"
    assert_no_overlap(buttons)


def _assert_floor(page: Page, where: str) -> None:
    """Sweep every rendered control and assert each meets the 44px floor."""
    rects = page.evaluate(_SWEEP, _CONTROLS)
    assert rects, f"{where}: no controls measured — wrong selector?"
    # 43.99, not 44: a 36px control grown by 2x4 lands on 43.999… in WebKit's
    # fractional layout, which is the floor met, not missed.
    under = [
        f"{r['what']} {r['w']:.1f}x{r['h']:.1f}"
        for r in rects if r["w"] < 43.99 or r["h"] < 43.99
    ]
    assert not under, (
        f"{where}: {len(under)} control(s) under the 44px effective floor "
        "(#1124):\n  " + "\n  ".join(sorted(set(under)))
    )


@pytest.mark.parametrize("tab", _TABS)
def test_every_control_meets_the_44px_floor(
    authed_page: Page, base_url: str, tab: str
) -> None:
    page = authed_page
    _mock(page)
    # Controls that render only with data: the Life OS recap tile's launch
    # and a Settings passkey row's remove. Both sat at 30px tall, unseen by
    # this sweep while the mock hid them (#1190). Registered after _mock, so
    # these routes win.
    _json_route(page, re.compile(r".*/api/life-os/recap-status$"), {
        "available": True, "ledger_exists": True, "age_days": 3,
        "staleness": "fresh", "proposal_pending": False, "proposal_name": None,
    })
    _json_route(page, re.compile(r".*/api/webauthn/status$"), {
        "configured": True, "enrollment_open": False, "devices": [
            {"id": "d1", "label": "Synthetic phone", "added_at": "2026-01-01",
             "last_used": None},
        ],
    })
    # The Jobs agenda sheet's empty state carries an Add job action (#1201)
    # that sat at 38.6px (#1216); since #1438 it lives in the agenda sheet
    # opened from the Next up card, not on the tab. It renders only when the
    # next 7 days hold no runs, which the unmocked agenda decided from the checkout's own
    # jobs.json and the clock: a worktree (whose job paths are blanked, so it
    # has no jobs) raced the 400ms settle below and a primary never showed
    # it. Pinned empty, it is measured on every run.
    _json_route(page, re.compile(r".*/api/jobs/agenda(\?.*)?$"), {
        "days": 7, "generated_epoch": 0, "occurrences": [], "frequent": [],
    })
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator(tab).click()
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    if tab in _DATA_CONTROL:
        expect(page.locator(_DATA_CONTROL[tab])).to_be_visible()
    page.wait_for_timeout(400)

    _assert_floor(page, tab)

    if tab == "#tabJobs":
        # The agenda sheet is a closed dialog until opened (#1438), so open
        # it and sweep what it shows, its empty-state Add job primary
        # included (#1216).
        page.locator("#jobsAgendaOpen").click()
        expect(page.locator("#jobsAgendaSheet")).to_be_visible()
        expect(page.locator("#jobsAgendaBody .empty-state-action")).to_be_visible()
        page.wait_for_timeout(200)
        _assert_floor(page, "Jobs > agenda sheet")
        page.locator("#jobsAgendaSheetClose").click()
        expect(page.locator("#jobsAgendaSheet")).to_be_hidden()

    if tab == _SETTINGS:
        # The sheets are closed dialogs above, so open each and sweep it.
        for sheet_id in _SHEETS:
            open_settings_sheet(page, sheet_id)
            if sheet_id in _SHEET_DATA_CONTROL:
                expect(page.locator(_SHEET_DATA_CONTROL[sheet_id])).to_be_visible()
            page.wait_for_timeout(200)
            _assert_floor(page, f"Settings > {sheet_id}")
            close_settings_sheets(page)
        for agent_id in _AGENTS:
            open_agent_sheet(page, agent_id)
            page.wait_for_timeout(200)
            _assert_floor(page, f"Settings > Launch defaults > {agent_id}")
            close_settings_sheets(page)

    # The card toolbars' toggles carry visible labels (#1176) and wrap: no
    # two expanded targets there may share pixels, on a line or across one.
    toolbar = page.locator(".pane:not([hidden]) .card-toolbar button:visible")
    if toolbar.count() >= 2:
        assert_no_overlap(toolbar)

    if tab == "#tabApps":
        _assert_helper_is_wired_to_a_real_locator(page)
        _assert_cluster_does_not_overlap(page, "#appsList .action-row")
    elif tab == "#tabBoard":
        # The lane toolbar's filter and ↻ (#1436): no two expanded targets
        # may share pixels (#1174).
        _assert_cluster_does_not_overlap(page, ".board-toolbar")
        # Last, because it reloads the page at another size and text step:
        # Large text on a 320px phone is where a row runs out of room first
        # (#1182).
        page.evaluate("localStorage.setItem('app-launcher.textsize', 'large')")
        page.set_viewport_size({"width": 320, "height": 844})
        page.reload(wait_until="domcontentloaded")
        page.locator("#tabBoard").click()
        page.wait_for_timeout(400)
        assert_no_overlap(page.locator(".board-toolbar button:visible"))
