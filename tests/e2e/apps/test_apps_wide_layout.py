"""The Apps tab built out for the PC, its port probe gated and its loads
honest (#1505, step 5/6 of #1500).

Apps is a weekly PC surface, so at the wide query (1100px and a fine pointer,
like Code and Jobs) it is two columns right of the rail: the page (header,
Apps, Trays) on the left, and on the right what is up now (Running, Other
ports inline, a plain line when nothing runs), with a running app's Open,
Copy URL and Stop as visible buttons. The port probe runs only while the Apps
tab is up and the page is visible. A failed load is an error with Retry, never
a blank list or an endless "Checking…".

Every boot fetch the tab reads is route-mocked before navigation (#510): no
real app is launched, stopped or probed, and the stop endpoints are stubs.
All names, paths and ports are fictional.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.waits import (
    assert_unpainted_at_rest,
    flush_requests,
    stable_eval,
    wait_until,
)

pytestmark = pytest.mark.smoke

_APPS = {"scan_root": "C:\\stub", "apps": [
    {"id": "alpha-dash", "name": "Alpha Dashboard", "kind": "streamlit",
     "bat_path": "C:\\stub\\alpha\\run.bat", "added_at": "2026-01-01T00:00:00",
     "autostart": False},
    {"id": "beta-web", "name": "Beta Web with a rather long name for its row",
     "kind": "webapp", "bat_path": "C:\\stub\\beta\\webapp.bat",
     "added_at": "2026-01-01T00:00:00", "autostart": False},
    {"id": "omega-tray", "name": "Omega Tray", "kind": "tray",
     "bat_path": "C:\\stub\\omega\\tray.bat", "added_at": "2026-01-01T00:00:00",
     "autostart": True},
]}

_RUNNING = {"running": [
    {"app_id": "beta-web", "name": "Beta Web with a rather long name for its row",
     "kind": "webapp", "pid": 4242, "started_at": 1736179200, "port": 8601,
     "url": "https://pc.example.invalid:8601/", "alive": True},
]}

_PROBE = {"listeners": [
    {"port": 8601, "pid": 4243, "name": "python.exe", "exe": "python.exe",
     "cmdline": "uvicorn", "app": "Beta Web", "parent_port": None,
     "service": None, "url": None},
    {"port": 8800, "pid": 6000, "name": "node.exe", "exe": "node.exe",
     "cmdline": "node server.js", "app": "Gamma Node", "parent_port": None,
     "service": None, "url": None},
]}

_WIDTHS = (1280, 1440)
_PROBE_RE = re.compile(r".*/api/ports/probe$")


def _json(page: Page, pattern: str, body: dict, status: int = 200) -> None:
    page.route(
        re.compile(pattern),
        lambda route: route.fulfill(
            status=status, content_type="application/json", body=json.dumps(body)),
    )


def _mock(page: Page, running: dict = _RUNNING) -> None:
    _json(page, r".*/api/apps$", _APPS)
    _json(page, r".*/api/apps/running$", running)
    _json(page, r".*/api/ports/probe$", _PROBE)


def _wide(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})


@pytest.mark.parametrize("width", _WIDTHS)
def test_wide_apps_is_two_columns_list_then_pane(
    authed_page: Page, base_url: str, browser_name: str, width: int
) -> None:
    if browser_name != "chromium":
        pytest.skip("the wide layout needs a fine pointer: desktop Chromium only")
    page = authed_page
    _wide(page, width)
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    expect(page.locator("#runningAppsList li.action-row")).to_have_count(1, timeout=10_000)
    expect(page.locator("#appsList li.action-row")).to_have_count(2, timeout=10_000)
    expect(page.locator("#otherPortsCard #listenersList .listener-row")).to_have_count(2)

    m = stable_eval(page.locator("#paneApps"), """pane => {
      const box = el => el.getBoundingClientRect();
      const list = box(pane.querySelector('.apps-list-card'));
      const col = pane.querySelector('#appsNowCol');
      const now = box(col);
      const run = pane.querySelector('#runningAppsList li.action-row');
      const title = run.querySelector('.action-row-title');
      const de = document.documentElement;
      return {
        listRight: list.right, nowLeft: now.left, nowRight: now.right,
        headTop: box(pane.querySelector('.page-head')).top,
        nowTop: box(pane.querySelector('.running-apps-card')).top,
        vw: de.clientWidth,
        pageOverflow: de.scrollWidth > de.clientWidth,
        colOverflow: col.scrollWidth > col.clientWidth,
        appOverflow: pane.closest('.app').scrollWidth > pane.closest('.app').clientWidth,
        // Keyboard order is document order: the list, then the pane.
        listFirst: !!(pane.querySelector('.registered-trays-card')
          .compareDocumentPosition(col) & Node.DOCUMENT_POSITION_FOLLOWING),
        titleCut: title.scrollWidth > title.clientWidth + 1,
        verbs: Array.from(run.querySelectorAll('.running-app-actions button'))
          .map(b => { const r = box(b); return [b.getAttribute('aria-label'), r.width, r.height]; }),
      };
    }""")
    assert m["listRight"] <= m["nowLeft"] + 1, f"the columns overlap: {m}"
    # The fixed pane ends at the viewport's layout edge; the root's reserved
    # scrollbar gutter (scrollbar-gutter: stable) stays outside it.
    assert m["nowRight"] >= m["vw"] - 16, f"the pane does not reach the window edge: {m}"
    assert abs(m["headTop"] - m["nowTop"]) <= 2, f"the columns do not start together: {m}"
    assert not (m["pageOverflow"] or m["colOverflow"] or m["appOverflow"]), (
        f"horizontal overflow at {width}px: {m}")
    assert m["listFirst"], "the pane comes before the list in keyboard order"
    assert not m["titleCut"], f"a running app's title is clipped at {width}px"
    # Open, Copy URL and Stop are visible 44px buttons beside the kebab.
    assert [v[0] for v in m["verbs"]] == [
        "Open Beta Web with a rather long name for its row",
        "Copy Beta Web with a rather long name for its row URL",
        "Stop Beta Web with a rather long name for its row",
    ], m["verbs"]
    assert all(v[1] >= 44 and v[2] >= 44 for v in m["verbs"]), m["verbs"]
    # Plain verbs (fleet-config#1420): Stop is no longer outlined, and none of
    # the three is painted at rest, in light or dark.
    for verb in page.locator("#runningAppsList .running-app-actions button").all():
        assert_unpainted_at_rest(page, verb)
    # The sheet's drill row gives way to the inline card; the phone's empty
    # block and idle line stay away while something runs.
    expect(page.locator("#otherPortsRow")).to_be_hidden()
    expect(page.locator("#appsIdleLine")).to_be_hidden()


def test_wide_running_actions_stop_through_the_confirm(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("the wide layout needs a fine pointer: desktop Chromium only")
    page = authed_page
    _wide(page, 1280)
    _mock(page)
    stops: list = []

    def _stop(route):
        stops.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"stopped": True}))

    page.route(re.compile(r".*/api/apps/[^/]+/instances/\d+/stop$"), _stop)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    row = page.locator("#runningAppsList li.action-row")
    expect(row).to_have_count(1, timeout=10_000)

    row.locator(".running-app-stop-inline").click()
    confirm = page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    page.keyboard.press("Escape")
    expect(confirm).to_be_hidden()
    flush_requests(page)
    assert stops == [], f"a dismissed confirm still stopped: {stops!r}"
    row.locator(".running-app-stop-inline").click()
    confirm.locator("#confirmDialogOk").click()
    wait_until(page, lambda: len(stops) == 1, "the confirmed Stop")
    assert stops[0].endswith("/api/apps/beta-web/instances/4242/stop"), stops


def test_wide_idle_column_says_nothing_runs_with_the_app_count(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("the wide layout needs a fine pointer: desktop Chromium only")
    page = authed_page
    _wide(page, 1440)
    _mock(page, running={"running": []})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    line = page.locator("#appsIdleLine")
    expect(line).to_have_text("Nothing running · 2 apps to start")
    # One plain line: the phone's empty block and footnote stay away, and the
    # line sits on the card's own inset, level with its title.
    expect(page.locator("#runningAppsEmpty")).to_be_hidden()
    expect(page.locator("#runningAppsNote")).to_be_hidden()
    m = stable_eval(page.locator(".running-apps-card"), """card => {
      const title = card.querySelector('.collapse-main').getBoundingClientRect().left;
      const range = document.createRange();
      range.selectNodeContents(card.querySelector('#appsIdleLine'));
      return { title: title, text: range.getBoundingClientRect().left }; }""")
    assert abs(m["title"] - m["text"]) <= 2, f"the idle line is not on the card's inset: {m}"


@pytest.mark.iphone
def test_phone_keeps_running_first_and_no_visible_actions(
    authed_page: Page, base_url: str
) -> None:
    """Below the wide query nothing moves: Running first, its actions in the
    kebab, Other ports behind the sheet."""
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    row = page.locator("#runningAppsList li.action-row")
    expect(row).to_have_count(1, timeout=10_000)
    expect(row.locator(".running-app-actions")).to_be_hidden()
    expect(page.locator("#paneApps > :not(.page-head)").first).to_have_id("appsNowCol")
    expect(page.locator("#otherPortsRow")).to_be_visible()
    expect(page.locator("#otherPortsCard")).to_be_hidden()
    m = stable_eval(page.locator("#paneApps"), """pane => {
      const de = document.documentElement;
      return de.scrollWidth > de.clientWidth; }""")
    assert m is False, "the phone Apps tab overflows sideways"


def test_probe_runs_only_on_a_visible_apps_tab(authed_page: Page, base_url: str) -> None:
    """#1505: nothing reads the listeners outside Apps, so the 5s probe asks
    nothing while another tab is up or the page is hidden, and asks once the
    moment Apps opens (#1376: driven by the fake clock, counted at the route)."""
    page = authed_page
    probes: list = []
    _json(page, r".*/api/apps$", _APPS)
    _json(page, r".*/api/apps/running$", _RUNNING)

    def _probe(route):
        probes.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_PROBE))

    page.route(_PROBE_RE, _probe)
    _fake_clock.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _fake_clock.wait_for_interval(page, 5000, "fetchListeners")

    # Booted on another tab: three probe intervals pass, no probe.
    _fake_clock.advance(page, 15_000)
    assert probes == [], f"probed off the Apps tab: {probes!r}"

    # Opening Apps probes at once, then on the 5s cadence.
    page.locator("#tabApps").click()
    wait_until(page, lambda: len(probes) == 1, "the probe on opening Apps")
    _fake_clock.advance(page, 5_000)
    assert len(probes) == 2, f"the 5s cadence on Apps: {probes!r}"

    # A hidden page asks nothing, even on Apps.
    page.evaluate("""() => Object.defineProperty(document, 'hidden',
      { configurable: true, get: () => true })""")
    _fake_clock.advance(page, 15_000)
    assert len(probes) == 2, f"probed while the page was hidden: {probes!r}"
    page.evaluate("() => { delete document.hidden; }")

    # Another tab again: quiet.
    page.locator("#tabJobs").click()
    _fake_clock.advance(page, 15_000)
    assert len(probes) == 2, f"probed after leaving Apps: {probes!r}"


@pytest.mark.parametrize("layout", ["phone", "wide"])
def test_failed_loads_show_an_error_with_retry(
    authed_page: Page, base_url: str, browser_name: str, layout: str
) -> None:
    """A failed Apps, Running or Trays load is the error state with Retry, and
    a failed probe reads "Could not check" (the row) with Retry (the list),
    never "Checking…" forever. Retry reads again and the lists come back."""
    if layout == "wide" and browser_name != "chromium":
        pytest.skip("the wide layout needs a fine pointer: desktop Chromium only")
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844} if layout == "phone"
                           else {"width": 1280, "height": 900})
    healthy = {"on": False}

    def _either(body):
        def handler(route):
            if healthy["on"]:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(body))
            else:
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"detail": "stub failure"}))
        return handler

    page.route(re.compile(r".*/api/apps$"), _either(_APPS))
    page.route(re.compile(r".*/api/apps/running$"), _either(_RUNNING))
    page.route(_PROBE_RE, _either(_PROBE))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()

    for host, text in (("#appsState", "Could not load the apps."),
                       ("#runningAppsState", "Could not load the running apps."),
                       ("#listenersState", "Could not check the ports.")):
        state = page.locator(host)
        expect(state).to_have_attribute("data-state", "error", timeout=10_000)
        expect(state.locator(".empty-state-message")).to_have_text(text)
        expect(state.locator(".async-retry")).to_have_count(1)
    trays = page.locator("#traysState")
    expect(trays).to_have_attribute("data-state", "error")
    expect(trays.locator(".empty-state-message")).to_have_text("Could not load the trays.")
    expect(page.locator("#runningAppsEmpty")).to_be_hidden()
    if layout == "phone":
        expect(page.locator("#otherPortsMeta")).to_have_text("Could not check")
    else:
        expect(page.locator("#listenersState")).to_be_visible()

    healthy["on"] = True
    page.locator("#appsState .async-retry").click()
    expect(page.locator("#appsList li.action-row")).to_have_count(2)
    expect(page.locator("#traysState")).to_be_hidden()
    page.locator("#runningAppsState .async-retry").click()
    expect(page.locator("#runningAppsList li.action-row")).to_have_count(1)
    if layout == "phone":
        # The row's tap opens the sheet, which probes again.
        page.locator("#otherPortsRow").click()
    else:
        page.locator("#listenersState .async-retry").click()
    expect(page.locator("#listenersList .listener-row")).to_have_count(2)
    expect(page.locator("#listenersState")).to_be_hidden()
    expect(page.locator("#otherPortsMeta")).to_have_text("1 listener not started here")
