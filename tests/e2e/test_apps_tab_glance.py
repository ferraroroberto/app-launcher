"""The Apps tab on the Glance system (#1437, step 5/7 of #1432).

One Running card (launched apps with the kind avatar and its alive badge,
then an "Other ports" row opening the listener sheet), the Apps list open
by default with live badges, Rename and Remove in every app's kebab behind
the vendored confirm sheet, Scan for new apps as the Apps card's last row,
and the page header's "N down" exception. Every boot fetch the tab reads is
route-mocked before navigation (#510): no real app is started, stopped or
probed. All names, paths and ports are fictional.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import flush_requests, stable_eval, wait_until

pytestmark = pytest.mark.smoke

_APPS = {
    "scan_root": "C:\\stub",
    "apps": [
        {"id": "alpha-dash", "name": "Alpha Dashboard", "kind": "streamlit",
         "bat_path": "C:\\stub\\alpha\\run_streamlit.bat",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "beta-web", "name": "Beta Web", "kind": "webapp",
         "bat_path": "C:\\stub\\beta\\webapp.bat",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "gamma-tunnel", "name": "Gamma Tunnel", "kind": "tunnel",
         "bat_path": "C:\\stub\\gamma\\webapp_tunnel.bat",
         "tunnel_url": "https://gamma.example.invalid/", "health": "up",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "delta-tunnel", "name": "Delta Tunnel", "kind": "tunnel",
         "bat_path": "C:\\stub\\delta\\webapp_tunnel.bat",
         "tunnel_url": "https://delta.example.invalid/", "health": "down",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "omega-tray", "name": "Omega Tray", "kind": "tray",
         "bat_path": "C:\\stub\\omega\\tray.bat",
         "added_at": "2026-01-01T00:00:00", "autostart": True},
        {"id": "sigma-tray", "name": "Sigma Tray", "kind": "tray",
         "bat_path": "C:\\stub\\sigma\\tray.bat",
         "added_at": "2026-01-01T00:00:00", "autostart": False},
    ],
}

_RUNNING = {"running": [
    {"app_id": "beta-web", "name": "Beta Web", "kind": "webapp", "pid": 4242,
     "started_at": 1736179200, "port": 8601,
     "url": "https://pc.example.invalid:8601/", "alive": True},
]}

# Three top-level listeners (one with a helper folded under it): 8601 is the
# app launched here, so two are "not started here".
_PROBE = {"listeners": [
    {"port": 8601, "pid": 4243, "name": "python.exe", "exe": "python.exe",
     "cmdline": "uvicorn", "app": "Beta Web", "parent_port": None,
     "service": None, "url": None},
    {"port": 8700, "pid": 5000, "name": "python.exe", "exe": "python.exe",
     "cmdline": "python -m hub", "app": "Hub", "parent_port": None,
     "service": None, "url": None},
    {"port": 8701, "pid": 5001, "name": "python.exe", "exe": "python.exe",
     "cmdline": "python -m hub.helper", "app": "Hub", "parent_port": 8700,
     "service": "hub.helper", "url": None},
    {"port": 8800, "pid": 6000, "name": "node.exe", "exe": "node.exe",
     "cmdline": "node server.js", "app": None, "parent_port": None,
     "service": None, "url": None},
]}


def _json(page: Page, pattern: str, body: dict) -> None:
    page.route(
        re.compile(pattern),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)),
    )


def _open_apps(page: Page, base_url: str, apps: dict = _APPS) -> None:
    _json(page, r".*/api/apps$", apps)
    _json(page, r".*/api/apps/running$", _RUNNING)
    _json(page, r".*/api/ports/probe$", _PROBE)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    expect(page.locator("#runningAppsList li.action-row")).to_have_count(1, timeout=10_000)


def _app_row(page: Page, app_id: str):
    return page.locator(f'#appsList li.action-row[data-id="{app_id}"]')


def test_running_card_badges_header_and_other_ports(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open_apps(page, base_url)

    # The header: a down tunnel is the exception, in its danger tone.
    head = page.locator("#appsHeadStatus")
    expect(head).to_have_text("1 down")
    expect(head.locator(".head-exception")).to_have_attribute("data-tone", "danger")

    # The Running card is first and open; the Port listeners card is gone.
    cards = page.locator("#paneApps > details")
    expect(cards.first).to_have_class(re.compile(r"\brunning-apps-card\b"))
    expect(page.locator("#paneApps .listeners-card")).to_have_count(0)

    # Other ports counts the top-level listeners the launcher did not start.
    expect(page.locator("#otherPortsMeta")).to_have_text("2 listeners not started here")
    page.locator("#otherPortsRow").click()
    sheet = page.locator("#otherPortsSheet")
    expect(sheet).to_be_visible()
    # The full grouped list: three top-level rows, the helper folded away.
    expect(sheet.locator("#listenersList .listener-row:not(.child)")).to_have_count(3)
    expect(sheet.locator("#listenersList .listener-row.expandable")).to_have_count(1)
    # Same kebab as before, Stop process last.
    row = sheet.locator("#listenersList .listener-row").first
    row.locator(".action-row-kebab").click()
    expect(row.locator(".row-menu > *").last).to_have_class(re.compile(r"\blistener-kill\b"))
    # Escape closes the menu only, not the sheet under it.
    page.keyboard.press("Escape")
    expect(row.locator(".row-menu")).to_be_hidden()
    expect(sheet).to_be_visible()
    # Check again is the sheet's one primary action, and re-probes.
    probes: list = []
    page.on("request", lambda r: probes.append(r.url) if r.url.endswith("/api/ports/probe") else None)
    sheet.locator("#listenersCheckAgain").click()
    wait_until(page, lambda: len(probes) >= 1, "Check again to re-probe the ports")
    expect(sheet.locator(".detail-actions button")).to_have_count(1)
    sheet.locator("#otherPortsSheetClose").click()
    expect(sheet).to_be_hidden()


def test_apps_list_is_open_with_live_badges_and_scan_row(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open_apps(page, base_url)

    card = page.locator("#paneApps details.apps-list-card")
    expect(card).to_have_attribute("open", "")
    # Trays are not apps: four rows here.
    expect(page.locator("#appsList li.action-row")).to_have_count(4)

    # Kind avatars: Streamlit gauge, webapp globe, tunnel cloud.
    for app_id, glyph in (("alpha-dash", "#i-gauge"), ("beta-web", "#i-globe"),
                          ("gamma-tunnel", "#i-cloud")):
        expect(_app_row(page, app_id).locator(".avatar use")).to_have_attribute("href", glyph)

    # The badge joins the running list: Beta Web runs, Alpha does not.
    expect(_app_row(page, "beta-web").locator(".avatar")).to_have_attribute("data-badge", "alive")
    expect(_app_row(page, "alpha-dash").locator(".avatar")).to_have_attribute("data-badge", "none")
    # An up tunnel is alive with no chip; a down one is red with a danger chip.
    up = _app_row(page, "gamma-tunnel")
    expect(up.locator(".avatar")).to_have_attribute("data-badge", "alive")
    expect(up.locator(".chip")).to_have_count(0)
    expect(up.locator(".action-row-meta")).to_have_text("Tunnel")
    down = _app_row(page, "delta-tunnel")
    expect(down.locator(".avatar")).to_have_attribute("data-badge", "down")
    expect(down.locator(".chip")).to_have_text("down")
    expect(down.locator(".chip")).to_have_attribute("data-tone", "danger")

    # Scan for new apps is the card's last row, and gone from Settings.
    expect(card.locator(".collapse-body > *:visible").last).to_have_id("appsScanRow")
    expect(page.locator("#rescanBtn")).to_have_count(0)
    _json(page, r".*/api/apps/scan$", {"new": []})
    page.locator("#appsScanRow").click()
    expect(page.locator("#scanDialog")).to_be_visible()
    page.locator("#scanCancel").click()

    # Trays: the summary says how many and how many autostart; a tray's line
    # says it starts at log on, and never claims "not running".
    expect(page.locator("#traysSummaryMeta")).to_have_text("2 · 1 autostart")
    page.locator(".registered-trays-card summary").click()
    omega = page.locator('#registeredTraysList li.action-row[data-id="omega-tray"]')
    expect(omega.locator(".action-row-meta")).to_have_text("starts at log on")
    sigma = page.locator('#registeredTraysList li.action-row[data-id="sigma-tray"]')
    expect(sigma.locator(".action-row-meta")).to_have_count(0)


def test_header_counts_apps_without_trays_when_nothing_is_down(
    authed_page: Page, base_url: str
) -> None:
    apps = {"scan_root": "C:\\stub", "apps": [
        a for a in _APPS["apps"] if a["id"] != "delta-tunnel"]}
    _open_apps(authed_page, base_url, apps)
    head = authed_page.locator("#appsHeadStatus")
    # Three apps (two trays excluded), one running.
    expect(head).to_have_text("3 apps · 1 running")
    expect(head.locator(".head-exception")).to_have_count(0)


def test_remove_asks_through_the_vendored_dialog(authed_page: Page, base_url: str) -> None:
    page = authed_page
    deletes: list = []

    def _delete(route):
        if route.request.method != "DELETE":
            route.fallback()
            return
        deletes.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"removed": "alpha-dash"}))

    page.route(re.compile(r".*/api/apps/alpha-dash$"), _delete)
    native: list = []
    page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    _open_apps(page, base_url)

    row = _app_row(page, "alpha-dash")
    row.locator(".action-row-kebab").click()
    row.locator(".app-remove-btn").click()
    confirm = page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    expect(confirm.locator("#confirmDialogTitle")).to_have_text("Remove Alpha Dashboard?")
    # Escape is a "no".
    page.keyboard.press("Escape")
    expect(confirm).to_be_hidden()
    flush_requests(page)
    assert deletes == [], f"a dismissed confirm still removed: {deletes!r}"

    row.locator(".action-row-kebab").click()
    row.locator(".app-remove-btn").click()
    confirm.locator("#confirmDialogOk").click()
    wait_until(page, lambda: len(deletes) == 1, "the Remove DELETE")
    assert native == [], f"a native dialog fired: {native!r}"


@pytest.mark.iphone
def test_drill_rows_and_menu_header_fit_the_phone(authed_page: Page, base_url: str) -> None:
    """The new rows keep the 44px floor, avatar rows take the two-line row
    height, and a long bat path in the menu's header wraps instead of
    widening the menu off a 390px screen."""
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    long_path = "C:\\stub\\" + "a-very-long-folder-name\\" * 6 + "run_streamlit.bat"
    apps = json.loads(json.dumps(_APPS))
    apps["apps"][0]["bat_path"] = long_path
    _open_apps(page, base_url, apps)

    for sel in ("#otherPortsRow", "#appsScanRow"):
        h = stable_eval(page.locator(sel), "el => el.getBoundingClientRect().height")
        assert h is not None and h >= 44, f"{sel} is {h}px tall, under the 44px floor"
    h = stable_eval(_app_row(page, "alpha-dash"), "el => el.getBoundingClientRect().height")
    assert h is not None and h >= 60, f"an avatar row is {h}px, not rows.lg"

    row = _app_row(page, "alpha-dash")
    row.locator(".action-row-kebab").click()
    menu = row.locator(".row-menu")
    expect(menu.locator(".row-menu-head")).to_have_text(long_path)
    box = stable_eval(menu, "el => { const r = el.getBoundingClientRect();"
                            " return { left: r.left, right: r.right }; }")
    assert box is not None and box["left"] >= 0 and box["right"] <= 390, (
        f"the menu runs off the screen: {box}")
