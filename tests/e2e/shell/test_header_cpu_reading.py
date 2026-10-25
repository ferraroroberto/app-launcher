"""Regression pin for #1522: the page header's CPU reading.

Every tab's header carries a small CPU reading left of the theme toggle: a
muted number at rest, attention after a sustained high, danger when pegged,
"–" when the server could not read it (never "0%"). One tap opens a sheet
with total CPU, memory and the busiest three processes. The poll is one per
page, never per tab, and stops while the page is hidden.

The level is the server's (tests/unit/usage/test_system_load.py pins the rules); this
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
from tests.e2e.fixtures.waits import boot_retrying_static_failure, flush_requests, stable_eval
from tests.e2e.code.test_code_tab_glance import _STALLED, _WAITING
from tests.e2e.code.test_code_tab_glance import _mock as _mock_sessions

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


def _boot(page: Page, base_url: str) -> None:
    """Open the page, and open it again if a ``/static/`` file failed (#1610).

    The reading's markup default is ``–`` / ``data-level="unknown"``; only
    ``system-load.js`` running ever changes it. A module that fails to load
    under gate load stops ``main.js`` outright, so the header stays at its
    default for the page's life and no wait recovers it.
    """
    boot_retrying_static_failure(
        page, lambda: page.goto(f"{base_url}/", wait_until="domcontentloaded")
    )


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
    _boot(page, base_url)

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


@pytest.mark.parametrize("width", [390, 1280])
def test_a_quiet_reading_yields_to_the_header_exceptions_only_where_room_is_missing(
    authed_page: Page, base_url: str, width: int
) -> None:
    """At 390px the header line's two exception parts need the reading's
    room, and no ellipsis may cut them (design.md page-header). A quiet
    reading steps aside there; a hot one is itself the exception and stays.
    At 1280px both fit, so the quiet one stays (no flash-then-hide)."""
    page = authed_page
    page.set_viewport_size({"width": width, "height": 844})
    current = {"body": _reading("normal", 30.0)}
    _route_load(page, current, [])
    _mock_sessions(page, [_WAITING, _STALLED])
    _boot(page, base_url)

    status = page.locator("#homeHeadStatus")
    reading = page.locator("#paneClaude > .home-head .cpu-reading")
    expect(status).to_have_text("1 stalled · 1 needs you")
    expect(reading).to_have_text("30%")
    (expect(reading).to_be_hidden if width < 500 else expect(reading).to_be_visible)()
    assert stable_eval(status, "(el) => el.scrollWidth - el.clientWidth") <= 0

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
    _boot(page, base_url)

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


def test_the_sheet_groups_a_burn_and_shows_what_the_rows_leave_out(
    authed_page: Page, base_url: str
) -> None:
    """#1646: 16 burners are one row with their count, a lone process keeps a
    plain row (never "×1"), and "Everything else" closes the gap to the CPU
    line. A remainder that rounds to nothing draws no row."""
    page = authed_page
    body_json = _reading("danger", 100.0)
    body_json["top"] = [
        {"name": "python", "cores": 11.2, "count": 16, "owner": "fleet-config-wt-1440"},
        {"name": "vmmemWSL", "cores": 1.6, "count": 1, "owner": None},
        {"name": "MsMpEng", "cores": 0.9, "count": 1, "owner": None},
    ]
    body_json["other_cores"] = 2.3  # 16.0 - 13.7
    current = {"body": body_json}
    _route_load(page, current, [])
    _fake_clock.install(page)
    _boot(page, base_url)

    page.locator("#paneClaude > .home-head .cpu-reading").click()
    body = page.locator("#cpuSheetBody")
    first = body.locator("[data-row='top-0']")
    expect(first).to_contain_text("python ×16 · fleet-config-wt-1440")
    expect(first).to_contain_text("11.2 cores")
    for lone in ("top-1", "top-2"):
        expect(body.locator(f"[data-row='{lone}']")).not_to_contain_text("×")
    other = body.locator("[data-row='other']")
    expect(other).to_have_text(re.compile(r"Everything else\s*2\.3 cores"))
    expect(other).to_have_class(re.compile(r"usage-sheet-row"))  # the sheet's own row style
    # The remainder is the last row, after the processes it completes.
    expect(body.locator("[data-row]").last).to_have_attribute("data-row", "other")

    # Nothing left over: no row, rather than "0 cores".
    current["body"] = {**body_json, "other_cores": 0.0}
    page.locator("#cpuSheetDone").click()
    _poll(page)
    page.locator("#paneClaude > .home-head .cpu-reading").click()
    expect(page.locator("#cpuSheetBody [data-row='top-0']")).to_be_visible()
    expect(page.locator("#cpuSheetBody [data-row='other']")).to_have_count(0)


def test_an_unreadable_sample_says_why_in_the_sheet(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _route_load(page, {"body": _reading("unknown", None, reason="stale")}, [])
    _boot(page, base_url)
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
    _boot(page, base_url)
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


def test_a_failed_script_load_gets_one_fresh_boot(authed_page: Page, base_url: str) -> None:
    """#1610: the first load of ``system-load.js`` is cut. The page then boots
    without ``main.js`` and the header keeps its markup default; the second
    boot is the one the reading comes from."""
    page = authed_page
    _route_load(page, {"body": _reading("normal", 42.4)}, [])
    navigations: list = []
    cut: list = []
    page.on("framenavigated", lambda f: navigations.append(1) if f == page.main_frame else None)

    def handle(route):
        # The page asks for the file more than once per load, so cut every ask
        # of the first load, not just the first ask.
        if len(navigations) <= 1:
            cut.append(1)
            route.abort()
        else:
            route.continue_()

    page.route(re.compile(r".*/static/system-load\.js"), handle)
    _boot(page, base_url)
    assert cut, "the script was never requested"
    expect(page.locator("#paneClaude > .home-head .cpu-reading")).to_have_text("42%")
