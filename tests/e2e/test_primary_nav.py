"""Regression pin for issue #263 — responsive primary navigation.

The launcher keeps one DOM nav: top segmented control on desktop/fine-pointer
screens, floating bottom tab bar on the iPhone projection. The same test also
pins the tab ARIA state and the terminal-overlay hide rule for the mobile bar.

Its WebKit branch also carries three iPhone-projection-only checks that used
to be their own modules, each skipping its Chromium node (folded in by #1215,
each kept verbatim in a helper below with its original docstring):

* ``test_iphone_revalidate.py`` — the ``/`` document's ``Cache-Control``
  reaches WebKit intact (commit 696b723);
* ``test_viewport.py`` — the WebKit projection really is iPhone-shaped
  (issue #31);
* ``test_bottom_tab_bar.py`` — the floating bottom tab bar is drift-free and
  baseline-stable (issue #267).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Response, expect

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

TAB_ORDER = [
    "tabBoard",
    "tabClaude",
    "tabLifeOS",
    "tabApps",
    "tabJobs",
]

# Matches playwright.devices["iPhone 15 Pro Max"]["viewport"]["width"].
_IPHONE_15_PRO_MAX_WIDTH = 430

BOTTOM_BAR_TAB_IDS = [
    "#tabClaude", "#tabApps", "#tabJobs", "#tabLifeOS", "#tabBoard",
]


def _capture_index_cache_control(page: Page, base_url: str) -> dict:
    """WebKit-projection pin for commit 696b723 (iPhone index revalidation).

    Formerly ``test_iphone_revalidate.py::test_index_cache_control_visible_to_webkit``.

    The bug: iOS Safari (especially PWA-installed) used to serve a stale
    ``index.html`` and request a ``?v=<old hash>`` script that no longer
    existed. The fix added ``Cache-Control: no-cache, must-revalidate`` to
    the index response so Safari issues a conditional GET on every load.

    This is a thin WebKit-specific check that the header actually
    reaches the browser through the real network stack (i.e. it isn't
    stripped by a proxy/middleware ordering bug). The non-browser
    ``test_cache_busting`` already pins the header at the HTTP level for
    both projections; this one runs through the rendering engine so a
    WebKit-specific regression surfaces here.

    Registers the listener; must run before ``goto``. Returns the dict the
    listener fills, asserted by ``_assert_index_cache_control``.
    """
    captured: dict = {}

    def _on_response(res: Response) -> None:
        # First navigation response, which is the / document. Later
        # /api/* and /static/* responses also fire this handler but
        # we only care about the HTML root.
        if "cache-control" in captured:
            return
        url = res.url.rstrip("/")
        if url == base_url.rstrip("/") or url == base_url.rstrip("/") + "/":
            captured["cache-control"] = res.headers.get("cache-control", "")
            captured["status"] = res.status

    page.on("response", _on_response)
    return captured


def _assert_index_cache_control(page: Page, captured: dict) -> None:
    page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    assert captured.get("status") == 200, (
        f"GET / returned {captured.get('status')!r} under WebKit"
    )
    cc = captured.get("cache-control", "")
    assert "no-cache" in cc and "must-revalidate" in cc, (
        f"WebKit saw Cache-Control={cc!r} on /; the iPhone-stale-index fix "
        "(commit 696b723) regressed or was stripped by middleware ordering"
    )


def _assert_iphone_viewport_active(page: Page) -> None:
    """Sanity-check that the WebKit projection actually applies the iPhone
    descriptor.

    Formerly ``test_viewport.py::test_iphone_viewport_active_on_webkit``.

    Confirms `browser_context_args` in conftest.py merged in
    `playwright.devices["iPhone 15 Pro Max"]` — without this, the WebKit run
    would silently use a desktop viewport and test_smoke.py wouldn't actually
    be exercising an iPhone-shaped target (issue #31).
    """
    width = page.evaluate("window.innerWidth")
    assert width == _IPHONE_15_PRO_MAX_WIDTH, (
        f"expected iPhone 15 Pro Max width {_IPHONE_15_PRO_MAX_WIDTH}, got {width} — "
        "the device descriptor merge in conftest.py didn't take effect"
    )


def _assert_bottom_tab_bar_is_stable_and_low(page: Page) -> None:
    """Regression pin for issue #267 — floating bottom tab bar polish.

    Formerly ``test_bottom_tab_bar.py::test_bottom_tab_bar_is_stable_and_low``;
    expects a tab (Jobs) already opened so one pill is active (filled) — the
    one that used to jump.

    Follow-up to the #263 bar. Pins two fixes on the iPhone (WebKit)
    projection:

    1. **No scroll drift.** The bar is promoted to its own compositing layer
       and the vendored nav-tabs.css/_vendored/nav/nav-tabs.js (issue #355)
       keep it glued to the visual viewport bottom in a browser tab
       (standalone PWAs get no measured translate at all — CSS owns the
       position there). At a settled steady state that pin is a no-op, so
       the bar carries no residual translate — asserted via the computed
       transform being identity/none.
    2. **Active pill is baseline-stable from first paint.** Every pill is a
       fixed height, centred in a fixed grid row, so the four pills share one
       height and one top edge and the active (filled) pill never protrudes
       above the bar's top edge — independent of when page content paints.

    The bar's `bottom` offset is now the vendored nav-tabs.css's canonical
    `--bottom-tabs-margin` (21px default, issue #355) rather than
    app-launcher's own prior #267 tuning (a reduced safe-area fraction + 2px
    gap) — the convergence trade-off of adopting the fleet-standard geometry
    verbatim.

    Desktop / fine-pointer projections keep the static top control
    unchanged, so the assertions are WebKit-only.
    """
    expect(page.locator("#tabJobs")).to_have_attribute("aria-selected", "true")

    metrics = page.evaluate(
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
        BOTTOM_BAR_TAB_IDS,
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

    # 3. Bar sits at the vendored canonical --bottom-tabs-margin (21px
    #    default) from the bottom edge — headless has 0 safe-area-inset, so
    #    this is the margin alone, not app-launcher's old #267 tuning.
    assert metrics["bottom"] == pytest.approx(21, abs=1)
    assert metrics["navBottom"] <= metrics["viewportHeight"]


def test_primary_nav_is_responsive_and_accessible(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    # The desktop half pins the top control, which is below the wide layout's
    # 1100px breakpoint (#1135); at 1100px and up the nav is a left rail,
    # pinned by test_wide_layout.py.
    index_response: dict = {}
    if browser_name != "webkit":
        authed_page.set_viewport_size({"width": 1099, "height": 720})
    else:
        # iOS Safari is the original stale-index regression: the listener
        # has to be in place before the navigation it observes.
        index_response = _capture_index_cache_control(authed_page, base_url)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    if browser_name == "webkit":
        _assert_iphone_viewport_active(authed_page)
        _assert_index_cache_control(authed_page, index_response)
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=10_000
    )

    tabs = authed_page.locator("nav.tabs")
    expect(tabs).to_be_visible()
    expect(tabs).to_have_attribute("role", "tablist")
    assert tabs.locator(":scope > button.tab").evaluate_all(
        "buttons => buttons.map(button => button.id)"
    ) == TAB_ORDER
    expect(authed_page.locator("#paneClaude")).to_be_visible()
    expect(authed_page.locator("#tabClaude")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(tabs).to_have_attribute("data-active-tab", "claude")

    authed_page.locator("#tabJobs").click()
    expect(authed_page.locator("#paneJobs")).to_be_visible()
    expect(authed_page.locator("#paneClaude")).to_be_hidden()
    expect(authed_page.locator("#tabJobs")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(authed_page.locator("#tabClaude")).to_have_attribute(
        "aria-selected", "false"
    )
    expect(tabs).to_have_attribute("data-active-tab", "jobs")

    metrics = authed_page.evaluate(
        """() => {
          const nav = document.querySelector('nav.tabs');
          const app = document.querySelector('.app');
          const icon = document.querySelector('#tabJobs .tab-icon');
          const navStyle = getComputedStyle(nav);
          const appStyle = getComputedStyle(app);
          const iconStyle = getComputedStyle(icon);
          const rect = nav.getBoundingClientRect();
          return {
            position: navStyle.position,
            display: navStyle.display,
            bottom: navStyle.bottom,
            iconDisplay: iconStyle.display,
            paddingBottom: parseFloat(appStyle.paddingBottom),
            rectBottom: rect.bottom,
            rectTop: rect.top,
            viewportHeight: window.innerHeight,
          };
        }"""
    )

    if browser_name == "webkit":
        assert metrics["position"] == "fixed"
        assert metrics["display"] == "grid"
        assert metrics["iconDisplay"] == "block"
        assert metrics["paddingBottom"] >= 80
        assert metrics["rectBottom"] <= metrics["viewportHeight"]
        assert metrics["rectTop"] > metrics["viewportHeight"] / 2

        # Jobs is open, so one pill is active: the #267 bar metrics.
        _assert_bottom_tab_bar_is_stable_and_low(authed_page)

        # Last: fakes an open terminal overlay.
        authed_page.evaluate(
            """() => {
              document.getElementById('terminalOverlay').hidden = false;
              document.body.classList.add('terminal-open');
            }"""
        )
        expect(tabs).to_be_hidden()
    else:
        # sticky (issue #355): the vendored nav-tabs.css keeps the desktop
        # segmented control pinned to the top of the scroll container —
        # the fleet-standard behavior, not app-launcher's own prior static/
        # in-flow placement.
        assert metrics["position"] == "sticky"
        assert metrics["display"] == "flex"
        # icon shows (issue #421): the vendored nav-tabs.css now renders the
        # desktop segmented control's SVG glyph, like the mobile pill does,
        # per project-scaffolding#142 — no longer hidden outside coarse-pointer.
        assert metrics["iconDisplay"] == "block"
        assert metrics["paddingBottom"] < 100
