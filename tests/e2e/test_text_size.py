"""Regression pin for #1134: a text-size control in Settings.

The viewport meta locks pinch-zoom (the app reads as a native shell), so
until now there was no way to make the text bigger (WCAG 1.4.4). Settings
now carries the vendored text-size range-tab row: Small / Default / Large
scale the root font-size, so every rem size follows and px geometry (nav,
rows, hit targets) does not.

The pre-paint boot script stamps html[data-textsize] from
``app-launcher.textsize`` before any stylesheet is parsed, so a reload or a
PWA relaunch never paints at the wrong size first.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke

_TABS = ["#tabClaude", "#tabBoard", "#tabLifeOS", "#tabApps", "#tabJobs"]

# Records, on the first data-textsize stamp, how many stylesheets the parser
# had already reached. The boot script sits above every <link>, so a no-flash
# boot stamps at zero.
_RECORD_FIRST_STAMP = """
(() => {
  if (!sessionStorage.getItem('ts-probe-armed')) return;
  new MutationObserver((records, obs) => {
    for (const r of records) {
      if (r.attributeName === 'data-textsize') {
        window.__firstStamp = {
          size: document.documentElement.dataset.textsize,
          sheets: document.querySelectorAll('link[rel="stylesheet"]').length,
        };
        obs.disconnect();
        return;
      }
    }
  }).observe(document, { subtree: true, attributes: true, attributeFilter: ['data-textsize'] });
})();
"""


def _open_settings(page: Page) -> None:
    page.locator(".settings-open-btn:visible").first.click()
    expect(page.locator("#paneSettings")).to_be_visible()


def _assert_small_and_default_steps(page: Page) -> None:
    """Formerly ``test_small_and_default_steps``."""
    control = page.locator("#textSizeControl")
    control.locator("[data-textsize='small']").click()
    expect(page.locator("body")).to_have_css("font-size", "15px")
    control.locator("[data-textsize='default']").click()
    expect(page.locator("body")).to_have_css("font-size", "16px")
    assert page.evaluate("localStorage.getItem('app-launcher.textsize')") == "default"


def _assert_large_at_320px_never_scrolls_sideways(page: Page) -> None:
    """Formerly ``test_large_at_320px_never_scrolls_sideways``, which seeded
    Large through an init script; here the stored step is the Large this
    test just chose, and it reloads at 320px."""
    page.set_viewport_size({"width": 320, "height": 700})
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("body")).to_have_css("font-size", "18px")

    def overflow() -> object:
        return page.evaluate(
            "() => { const el = document.scrollingElement;"
            " return el.scrollWidth > 0 ? el.scrollWidth - el.clientWidth : null; }"
        )

    for tab in _TABS:
        page.locator(tab).click()
        page.wait_for_timeout(300)
        spill = stable_read(overflow)
        assert spill <= 0, f"{tab} at Large/320px scrolls sideways by {spill}px"

    _open_settings(page)
    spill = stable_read(overflow)
    assert spill <= 0, f"Settings at Large/320px scrolls sideways by {spill}px"


def test_large_sets_18px_body_and_survives_reload(
    authed_page: Page, base_url: str
) -> None:
    """The whole #1134 control on one page load (merged by #1215): the
    default step, Small and Default, Large surviving a reload with a no-flash
    first stamp, and Large at 320px never scrolling sideways."""
    page = authed_page
    page.add_init_script(_RECORD_FIRST_STAMP)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_settings(page)

    control = page.locator("#textSizeControl")
    expect(control.locator(".range-tab")).to_have_count(3)
    expect(control.locator("[data-textsize='default']")).to_have_attribute(
        "aria-pressed", "true"
    )

    _assert_small_and_default_steps(page)

    control.locator("[data-textsize='large']").click()
    expect(page.locator("html")).to_have_attribute("data-textsize", "large")
    expect(page.locator("body")).to_have_css("font-size", "18px")
    expect(control.locator("[data-textsize='large']")).to_have_attribute(
        "aria-pressed", "true"
    )

    page.evaluate("sessionStorage.setItem('ts-probe-armed', '1')")
    page.reload(wait_until="domcontentloaded")
    first = page.evaluate("window.__firstStamp || null")
    assert first == {"size": "large", "sheets": 0}, (
        f"first data-textsize stamp was {first!r}; the boot script must stamp "
        "the stored step before any stylesheet is parsed"
    )
    expect(page.locator("body")).to_have_css("font-size", "18px")

    # Last: resizes and reloads the page.
    _assert_large_at_320px_never_scrolls_sideways(page)
