"""Regression pin for #1507: a row menu never opens under the floating nav.

On the phone, the Jobs tab's ⋮ on a row near the bottom of the list opened
its menu downwards, behind the floating bottom-tab pill, and the lower items
could not be tapped. row-menu.js now keeps every open menu inside the band
above the pill: it opens on its CSS side when that fits, flips to the
anchor's other side when it would run under the nav, and only clamps (with an
internal scroll) when neither side fits. One rule for every caller; Jobs is
just where it was seen.

The pill only renders under ``(pointer: coarse) and (max-width: 520px)``, so
Chromium gets touch emulation like ``test_overlay_standalone_scrollable.py``.
"""
from __future__ import annotations

import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_eval

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_COUNT = 14


def _jobs() -> list:
    now = int(time.time())
    return [
        {
            "id": "job%02d" % i, "name": "Sample job %d" % i, "target_kind": "py",
            "schedule_chip": "daily 03:00", "running": False, "stuck": False,
            "paused": False, "args": "", "params": [], "confirm": False,
            "schedule": {"type": "daily", "at": "03:00"},
            "next_run_epoch": now + (i + 1) * 3600,
            "stats": {"last7": []},
        }
        for i in range(_COUNT)
    ]


def _open_jobs(page: Page, base_url: str, browser_name: str) -> None:
    if browser_name == "chromium":
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    body = _json.dumps({"jobs": _jobs()})
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    expect(page.locator("#jobsList li.job-row")).to_have_count(_COUNT)
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector('nav.tabs')).position"
    ) == "fixed", "the floating nav pill did not render; this test would prove nothing"


# One read of the open menu against its anchor and the nav: the menu's box,
# the kebab's, the pill's top, and whether each item is the topmost element
# at its own centre (i.e. tappable, not under the nav).
_MEASURE = """
(menu) => {
  const r = (el) => el.getBoundingClientRect();
  const m = r(menu);
  const kebab = r(menu.parentElement.querySelector('.action-row-kebab'));
  const nav = r(document.querySelector('nav.tabs'));
  const items = Array.from(menu.querySelectorAll('.row-menu-btn')).map((b) => {
    const br = r(b);
    const hit = document.elementFromPoint(br.left + br.width / 2, br.top + br.height / 2);
    return { label: b.getAttribute('aria-label'), hittable: !!hit && b.contains(hit) };
  });
  return { top: m.top, bottom: m.bottom, kebabTop: kebab.top,
           kebabBottom: kebab.bottom, navTop: nav.top, items: items };
}
"""


def _open_menu(page: Page, index: int):
    row = page.locator("#jobsList li.job-row").nth(index)
    row.locator(".action-row-kebab").click()
    menu = row.locator(".row-menu")
    expect(menu).to_be_visible()
    return menu


def _assert_clear_of_nav(m: dict, where: str) -> None:
    assert m["bottom"] <= m["navTop"], f"{where}: menu runs under the nav pill: {m}"
    assert m["top"] >= 0, f"{where}: menu runs off the top of the screen: {m}"
    blocked = [i["label"] for i in m["items"] if not i["hittable"]]
    assert not blocked, f"{where}: items not tappable: {blocked} ({m})"


def test_last_row_menu_opens_above_the_row_clear_of_the_nav(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    page = authed_page
    _open_jobs(page, base_url, browser_name)

    # A row near the top still opens downwards, as before.
    first = _open_menu(page, 0)
    m = stable_eval(first, _MEASURE)
    assert m["top"] >= m["kebabBottom"] - 1, f"top row's menu no longer opens below: {m}"
    _assert_clear_of_nav(m, "top row")
    page.keyboard.press("Escape")
    expect(first).to_be_hidden()

    # The last row, scrolled to the bottom of the list, sits just above the
    # pill: its menu flips above the row, whole and tappable.
    page.locator("#jobsList li.job-row").last.scroll_into_view_if_needed()
    page.evaluate("() => window.scrollTo(0, document.scrollingElement.scrollHeight)")
    last = _open_menu(page, _COUNT - 1)
    m = stable_eval(last, _MEASURE)
    _assert_clear_of_nav(m, "last row")
    assert m["bottom"] <= m["kebabTop"] + 1, f"last row's menu did not open above the row: {m}"
    page.keyboard.press("Escape")
    expect(last).to_be_hidden()


def test_open_menu_refits_when_scrolling_brings_its_row_down_to_the_nav(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """Re-placement on scroll still runs: a menu that fit below its row when
    it opened flips above once a scroll carries the row down to the pill."""
    page = authed_page
    _open_jobs(page, base_url, browser_name)
    page.evaluate("() => window.scrollTo(0, document.scrollingElement.scrollHeight)")

    # Pick the row whose menu still fits below it: the topmost visible one.
    index = page.evaluate(
        """() => Array.from(document.querySelectorAll('#jobsList li.job-row'))
             .findIndex((li) => li.getBoundingClientRect().top >= 80)"""
    )
    menu = _open_menu(page, index)
    m = stable_eval(menu, _MEASURE)
    assert m["top"] >= m["kebabBottom"] - 1, f"precondition: menu should open below: {m}"

    # Scroll up so the row travels down until it sits just above the pill.
    shift = page.evaluate(
        """(i) => {
          const li = document.querySelectorAll('#jobsList li.job-row')[i];
          const nav = document.querySelector('nav.tabs').getBoundingClientRect();
          return Math.round(nav.top - 8 - li.getBoundingClientRect().bottom);
        }""",
        index,
    )
    page.evaluate("(dy) => window.scrollBy(0, -dy)", shift)
    page.wait_for_function(
        """() => {
          const menu = document.querySelector('#jobsList .row-menu:not([hidden])');
          const nav = document.querySelector('nav.tabs').getBoundingClientRect();
          return !!menu && menu.getBoundingClientRect().bottom <= nav.top;
        }""",
        timeout=5_000,
    )
    m = stable_eval(menu, _MEASURE)
    _assert_clear_of_nav(m, "after scrolling")
    assert m["bottom"] <= m["kebabTop"] + 1, f"menu did not flip above its row: {m}"
