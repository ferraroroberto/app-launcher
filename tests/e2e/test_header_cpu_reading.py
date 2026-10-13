"""Regression pin for #1522: the page header's CPU reading.

Every tab's header carries a small CPU reading left of the theme toggle: a
muted number at rest, attention after a sustained high, danger when pegged,
"–" when the server could not read it (never "0%"). One tap opens a sheet
with total CPU, memory and the busiest three processes. The poll is one per
page, never per tab, and stops while the page is hidden.

The level is the server's (tests/test_system_load.py pins the rules); this
pins what the header draws for each, the sheet, and the hidden-page gate.
``/api/system/load`` is route-mocked with invented data before ``goto()``
and the poll is jumped on the fake clock (#1376), never waited out.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import flush_requests, stable_eval
from tests.e2e.test_code_tab_glance import _STALLED, _WAITING
from tests.e2e.test_code_tab_glance import _mock as _mock_sessions

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_POLL_MS = 10_000

_TABS = {
    "#tabClaude": "#paneClaude",
    "#tabApps": "#paneApps",
    "#tabJobs": "#paneJobs",
    "#tabLifeOS": "#paneLifeOS",
    "#tabBoard": "#paneBoard",
}


def _reading(level: str, cpu, reason=None) -> dict:
    known = cpu is not None
    return {
        "level": level,
        "reason": reason,
        "cpu_percent": cpu,
        "memory": {"used_bytes": 48 * 1024**3, "total_bytes": 64 * 1024**3, "percent": 75.0}
        if known else None,
        "top": [
            {"name": "python (pytest)", "cores": 3.1},
            {"name": "node (server)", "cores": 1.0},
            {"name": "builder", "cores": 0.42},
        ] if known else [],
        "cores": 16 if known else None,
        "age_seconds": 1.0 if known else None,
    }


def _route_load(page: Page, current: dict, calls: list) -> None:
    def handle(route):
        calls.append(1)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(current["body"]))

    page.route(re.compile(r".*/api/system/load$"), handle)


def _poll(page: Page) -> None:
    with page.expect_response(lambda r: r.url.endswith("/api/system/load")):
        _fake_clock.jump_interval(page, _POLL_MS, "fetchSystemLoad")


def _set_hidden(page: Page, hidden: bool) -> None:
    page.evaluate(
        """(hidden) => {
          Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
          document.dispatchEvent(new Event('visibilitychange'));
        }""",
        hidden,
    )


def test_every_header_shows_the_reading_and_its_four_states(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    current = {"body": _reading("normal", 42.4)}
    calls: list = []
    _route_load(page, current, calls)
    _fake_clock.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # One reading per page header, Settings' included.
    expect(page.locator(".home-head .cpu-reading")).to_have_count(6)

    # Every tab: the muted reading, left of the theme toggle, in the one row.
    for tab, pane in _TABS.items():
        page.locator(tab).click()
        reading = page.locator(f"{pane} > .home-head .cpu-reading")
        expect(reading).to_have_text("42%")
        expect(reading).to_have_attribute("data-level", "normal")
        boxes = stable_eval(
            page.locator(f"{pane} > .home-head"),
            """(head) => {
              const r = (sel) => head.querySelector(sel).getBoundingClientRect();
              const h = head.getBoundingClientRect();
              return { head: h.height, overflow: head.scrollWidth - head.clientWidth,
                       cpuRight: r('.cpu-reading').right, toggleLeft: r('.theme-toggle-btn').left,
                       gearRight: r('.settings-open-btn').right, headRight: h.right };
            }""",
        )
        assert boxes["head"] <= 60, f"{pane}: the header wrapped ({boxes['head']}px)"
        assert boxes["overflow"] <= 0, f"{pane}: the header overflows by {boxes['overflow']}px"
        assert boxes["cpuRight"] <= boxes["toggleLeft"], f"{pane}: the reading is not left of the toggle"
        assert boxes["gearRight"] <= boxes["headRight"], f"{pane}: the gear is pushed off the card"

    reading = page.locator("#paneBoard > .home-head .cpu-reading")
    muted = reading.evaluate("el => getComputedStyle(el).color")

    # Attention: the server says the high has held for ~30 s.
    current["body"] = _reading("attention", 76.0)
    _poll(page)
    expect(reading).to_have_text("76%")
    expect(reading).to_have_attribute("data-level", "attention")
    attention = reading.evaluate("el => getComputedStyle(el).color")
    assert attention != muted, "attention must ring in its tone, not stay muted"

    # Danger: pegged.
    current["body"] = _reading("danger", 97.0)
    _poll(page)
    expect(reading).to_have_text("97%")
    expect(reading).to_have_attribute("data-level", "danger")
    danger = reading.evaluate("el => getComputedStyle(el).color")
    assert danger not in (muted, attention), "danger must be its own tone"

    # Unknown: a dash, never 0%.
    current["body"] = _reading("unknown", None, reason="error")
    _poll(page)
    expect(reading).to_have_text("–")
    expect(reading).to_have_attribute("data-level", "unknown")

    # Back to normal: the colour goes quiet again.
    current["body"] = _reading("normal", 12.0)
    _poll(page)
    expect(reading).to_have_text("12%")
    expect(reading).to_have_attribute("data-level", "normal")


def test_a_quiet_reading_yields_to_the_header_exceptions_a_hot_one_does_not(
    authed_page: Page, base_url: str
) -> None:
    """At 390px the header line's two exception parts need the reading's
    room, and no ellipsis may cut them (design.md page-header). A quiet
    reading steps aside; a hot one is itself the exception and stays."""
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    current = {"body": _reading("normal", 30.0)}
    _route_load(page, current, [])
    _mock_sessions(page, [_WAITING, _STALLED])
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    status = page.locator("#homeHeadStatus")
    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    expect(status).to_have_text("1 stalled · 1 needs you")
    expect(reading).to_have_text("30%")
    expect(reading).to_be_hidden()

    current["body"] = _reading("danger", 96.0)
    with page.expect_response(lambda r: r.url.endswith("/api/system/load")):
        _set_hidden(page, False)  # a foreground return reads at once
    expect(reading).to_have_text("96%")
    expect(reading).to_be_visible()


def test_the_tap_sheet_lists_cpu_memory_and_the_top_three(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    current = {"body": _reading("danger", 93.0)}
    _route_load(page, current, [])
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    expect(reading).to_have_text("93%")
    reading.click()
    sheet = page.locator("#cpuSheet")
    expect(sheet).to_be_visible()
    body = sheet.locator("#cpuSheetBody")
    expect(body.locator("[data-row='cpu']")).to_contain_text("93% of 16 cores")
    expect(body.locator("[data-row='cpu'] .usage-sheet-value")).to_have_attribute("data-tone", "danger")
    expect(body.locator("[data-row='memory']")).to_contain_text("48 of 64 GB (75%)")
    expect(body.locator("[data-row^='top-']")).to_have_count(3)
    expect(body.locator("[data-row='top-0']")).to_contain_text("python (pytest)")
    expect(body.locator("[data-row='top-0']")).to_contain_text("3.1 cores")
    expect(body.locator("[data-row='top-1']")).to_contain_text("1 core")
    page.locator("#cpuSheetDone").click()
    expect(sheet).to_be_hidden()


def test_an_unreadable_sample_says_why_in_the_sheet(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _route_load(page, {"body": _reading("unknown", None, reason="stale")}, [])
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    expect(reading).to_have_text("–")
    reading.click()
    expect(page.locator("#cpuSheetBody [data-row='cpu']")).to_contain_text("Last reading is too old")
    expect(page.locator("#cpuSheetBody [data-row^='top-']")).to_have_count(0)
    page.locator("#cpuSheetClose").click()


def test_the_poll_stops_while_the_page_is_hidden(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    calls: list = []
    _route_load(page, {"body": _reading("normal", 20.0)}, calls)
    _fake_clock.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#paneClaude > .home-head .cpu-reading")).to_have_text("20%")
    _poll(page)
    seen = len(calls)

    # A tab switch adds no request of its own: one poll for the page.
    page.locator("#tabApps").click()
    page.locator("#tabBoard").click()
    flush_requests(page)
    assert len(calls) == seen, "a tab switch fetched the reading"

    # Hidden: two whole periods pass and nothing is asked.
    _set_hidden(page, True)
    _fake_clock.advance(page, _POLL_MS)
    _fake_clock.advance(page, _POLL_MS)
    flush_requests(page)
    assert len(calls) == seen, f"polled {len(calls) - seen}x while hidden"

    # Back in the foreground: one read at once.
    with page.expect_response(lambda r: r.url.endswith("/api/system/load")):
        _set_hidden(page, False)
    assert len(calls) == seen + 1
