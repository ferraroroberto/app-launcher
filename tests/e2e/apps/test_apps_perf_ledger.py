"""Apps tab: each app's phone-speed result and trend from the perf ledger.

The registry and ``/api/apps/perf`` are route-mocked with a fixture ledger, so
every state is drawn on demand. What this pins: a measured app shows its
result against budget, an over-budget one links its issues, and everything
that is not a measured pass (no record, an unmeasured run, an unreadable
ledger, a failed read) says so and is never drawn as within budget. Nothing
about it reaches the Board.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import stable_eval

pytestmark = pytest.mark.smoke

_APPS = {
    "scan_root": "C:\\stub",
    "apps": [
        {"id": "good-tray", "name": "Good App", "kind": "tray",
         "bat_path": "C:\\stub\\good-app\\tray.bat", "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "slow-web", "name": "Slow App", "kind": "webapp",
         "bat_path": "C:\\stub\\slow-app\\webapp.bat", "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "seeded-web", "name": "Seeded App", "kind": "webapp",
         "bat_path": "C:\\stub\\seeded-app\\webapp.bat", "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "partial-web", "name": "Partial App", "kind": "webapp",
         "bat_path": "C:\\stub\\partial-app\\webapp.bat", "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "never-web", "name": "Never Run App", "kind": "webapp",
         "bat_path": "C:\\stub\\never-app\\webapp.bat", "added_at": "2026-01-01T00:00:00", "autostart": False},
        {"id": "good-tunnel", "name": "Good Tunnel", "kind": "tunnel",
         "bat_path": "C:\\stub\\good-app\\webapp_tunnel.bat", "tunnel_url": "https://t.example.com/",
         "health": "up", "added_at": "2026-01-01T00:00:00", "autostart": False},
    ],
}


def _check(measured, budget, status="pass"):
    return {"measured": measured, "budget": budget, "status": status}


def _entry(state, **over):
    base = {
        "state": state, "run_id": "20261107T030000Z", "ts": "2026-11-07T03:00:00Z", "build": "abc1234",
        "seeded": False, "cold_ready_ms": None, "warm_ready_ms": None, "warm_bytes_kb": None,
        "fails": [], "trend": [], "issues_url": None,
    }
    base.update(over)
    return base


def _perf_ok() -> dict:
    return {
        "ledger": "ok",
        "rows": {"good-tray": "good-app", "slow-web": "slow-app", "seeded-web": "seeded-app",
                 "partial-web": "partial-app"},
        "apps": {
            "good-app": _entry(
                "pass", cold_ready_ms=_check(1800, 2500), warm_ready_ms=_check(600, 1500),
                warm_bytes_kb=_check(410, 800), trend=[2100, 1950, 1800],
            ),
            "slow-app": _entry(
                "over-budget", cold_ready_ms=_check(3100, 2500, "fail"), warm_ready_ms=_check(900, 1500),
                warm_bytes_kb=_check(410, 800), fails=["cold.ready_ms"], trend=[2200, 2600, 3100],
                issues_url="https://github.com/example/slow-app/issues?q=is%3Aissue+is%3Aopen+label%3Aperf-review",
            ),
            "seeded-app": _entry("pass", seeded=True),
            "partial-app": _entry("unmeasured"),
        },
    }


def _open_apps(page: Page, base_url: str, perf, status: int = 200) -> None:
    page.route("**/api/apps", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_APPS)))
    page.route("**/api/apps/running", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"running": []})))
    page.route("**/api/apps/perf", lambda r: r.fulfill(
        status=status, content_type="application/json", body=json.dumps(perf)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    for card in (".apps-list-card", ".registered-trays-card"):
        if page.locator(card).get_attribute("open") is None:
            page.locator(f"{card} summary").click()


def _block(page: Page, app_id: str):
    return page.locator(f'li.action-row[data-id="{app_id}"] .app-perf')


def test_measured_apps_show_result_against_budget_and_never_green_otherwise(
    authed_page: Page, base_url: str
) -> None:
    _open_apps(authed_page, base_url, _perf_ok())

    good = _block(authed_page, "good-tray")
    expect(good).to_have_attribute("data-state", "pass", timeout=5_000)
    expect(good).to_contain_text("Within budget")
    expect(good).to_contain_text("cold 1.8 s / 2.5 s")
    expect(good).to_contain_text("warm 600 ms / 1.5 s")
    expect(good).to_contain_text("transfer 410 KB / 800 KB")
    expect(good.locator("svg.app-perf-spark")).to_have_attribute(
        "aria-label", "Cold ready, last 3 runs: 2.1 s to 1.8 s")
    expect(authed_page.locator('li[data-id="good-tray"] .app-perf-issues')).to_have_count(0)

    slow = _block(authed_page, "slow-web")
    expect(slow).to_have_attribute("data-state", "over")
    expect(slow.locator(".chip")).to_have_text("Over budget")
    expect(slow).to_contain_text("cold ready")
    expect(slow.locator(".app-perf-num.is-over")).to_have_text("cold 3.1 s / 2.5 s")
    link = authed_page.locator('li[data-id="slow-web"] a.app-perf-issues')
    expect(link).to_have_attribute("href", "https://github.com/example/slow-app/issues?q=is%3Aissue+is%3Aopen+label%3Aperf-review")
    expect(link).to_have_attribute("target", "_blank")

    # A pass seeded from an issue log has no numbers and says so.
    seeded = _block(authed_page, "seeded-web")
    expect(seeded).to_contain_text("Passed, no timings yet")
    expect(seeded.locator(".app-perf-nums")).to_have_count(0)

    # An unmeasured run, and an app the ledger has never seen, are not a pass.
    for app_id in ("partial-web", "never-web"):
        block = _block(authed_page, app_id)
        expect(block).to_have_attribute("data-state", "none")
        expect(block.locator(".chip")).to_have_text("Not measured")
        expect(block).not_to_contain_text("Within budget")

    # A tunnel row is not the app's own web row.
    expect(_block(authed_page, "good-tunnel")).to_have_count(0)


@pytest.mark.parametrize(
    "perf,status",
    [
        ({"ledger": "unreadable", "apps": {}, "rows": {}}, 200),
        ({"detail": "boom"}, 500),
    ],
    ids=["unreadable-ledger", "failed-read"],
)
def test_an_unreadable_ledger_or_failed_read_is_unknown_never_green(
    authed_page: Page, base_url: str, perf, status
) -> None:
    _open_apps(authed_page, base_url, perf, status)
    for app_id in ("good-tray", "slow-web", "never-web"):
        block = _block(authed_page, app_id)
        expect(block).to_have_attribute("data-state", "unknown", timeout=5_000)
        expect(block.locator(".chip")).to_have_text("Unknown")
        expect(block).not_to_contain_text("Within budget")


def test_a_missing_ledger_is_not_measured_never_green(authed_page: Page, base_url: str) -> None:
    _open_apps(authed_page, base_url, {"ledger": "missing", "apps": {}, "rows": {}})
    block = _block(authed_page, "good-tray")
    expect(block.locator(".chip")).to_have_text("Not measured", timeout=5_000)
    expect(block).not_to_contain_text("Within budget")


@pytest.mark.iphone
def test_perf_block_fits_the_phone_width(authed_page: Page, base_url: str) -> None:
    _open_apps(authed_page, base_url, _perf_ok())
    row = authed_page.locator('li.action-row[data-id="slow-web"]')
    expect(row.locator(".app-perf")).to_be_visible(timeout=5_000)
    fits = stable_eval(
        row, "el => el.scrollWidth <= el.clientWidth + 1 && "
             "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert fits is True
    # The issues link keeps the 44px touch floor.
    height = stable_eval(row.locator("a.app-perf-issues"), "el => el.getBoundingClientRect().height")
    assert height is not None and height >= 44
