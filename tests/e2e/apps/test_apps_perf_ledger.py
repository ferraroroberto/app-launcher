"""Apps tab: the Performance card, from the perf ledger.

The registry and ``/api/apps/perf`` are route-mocked with a fixture ledger, so
every state is drawn on demand. What this pins: the card lists only measured
apps, each with its result against budget; an over-budget one links its
issues; every app without a result is listed under its reason; none of that
is drawn as within budget when the ledger is unreadable or a read fails; and
no app row carries a result of its own; and a hidden page reads no ledger.
Nothing about it reaches the Board.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.waits import flush_requests, stable_eval, wait_until

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
        {"id": "odd-web", "name": "Odd App", "kind": "webapp",
         "bat_path": "C:\\stub\\odd-app\\webapp.bat", "added_at": "2026-01-01T00:00:00", "autostart": False},
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
                 "partial-web": "partial-app", "odd-web": "odd-app"},
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
            "odd-app": _entry("unknown"),
        },
    }


def _open_apps(page: Page, base_url: str, perf, status: int = 200) -> dict:
    """Open the Apps tab's Performance card; return the perf answer's knobs.

    The route reads ``answer`` on every request, so a step can change what
    the next read sees and drive it with Retry or a tab re-open, which both
    force a re-read past the card's own throttle.
    """
    answer = {"perf": perf, "status": status}
    page.route("**/api/apps", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(_APPS)))
    page.route("**/api/apps/running", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"running": []})))
    page.route("**/api/apps/perf", lambda r: r.fulfill(
        status=answer["status"], content_type="application/json", body=json.dumps(answer["perf"])))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    page.locator(".apps-perf-card summary").click()
    return answer


def _row(page: Page, app_id: str):
    return page.locator(f'#perfMeasured li.perf-row[data-id="{app_id}"]')


def _reason(page: Page, reason: str):
    return page.locator(f'#perfUnmeasured .perf-reason[data-reason="{reason}"]')


def _assert_unknown(page: Page, message: str) -> None:
    """Unreadable or unread: Unknown with the reason and Retry, never green."""
    expect(page.locator("#perfState")).to_contain_text(message, timeout=5_000)
    expect(page.locator("#perfState .async-retry")).to_have_count(1)
    expect(page.locator("#perfSummaryMeta")).to_have_text("Unknown")
    expect(page.locator("#perfMeasured li")).to_have_count(0)
    expect(page.locator(".apps-perf-card")).not_to_contain_text("Within budget")
    expect(page.locator("#perfUnmeasured")).to_be_hidden()


def _assert_no_ledger_read_while_hidden(page: Page) -> None:
    """The ledger read follows the registry read. A registry answer that lands
    after the page hid starts none; the poll on the way back reads it."""
    hold, held, ledger_reads = [], [], []
    page.route("**/api/apps", lambda r: held.append(r) if hold else r.fallback())
    page.on("request", lambda r: r.url.endswith("/api/apps/perf") and ledger_reads.append(r.url))

    def _set_hidden(hidden: bool) -> None:
        page.evaluate("""(hidden) => {
          Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
          document.dispatchEvent(new Event('visibilitychange'));
        }""", hidden)

    _fake_clock.wait_for_interval(page, 4_000, "fetchApps")
    hold.append(True)
    _fake_clock.advance(page, 4_000)
    wait_until(page, lambda: bool(held), "a registry poll held open")
    # Past the card's own throttle, without firing a timer.
    page.clock.set_system_time(datetime.fromtimestamp(page.evaluate("Date.now()") / 1000 + 61))
    _set_hidden(True)
    hold.clear()
    held.pop().fulfill(status=200, content_type="application/json",
                       body=json.dumps(_APPS).replace("Good App", "Late App"))
    # The late name on the page proves the answer was handled, follow-up included.
    expect(page.locator("#registeredTraysList")).to_contain_text("Late App")
    flush_requests(page)
    assert not ledger_reads, "a ledger read started while the page was hidden"

    _set_hidden(False)
    wait_until(page, lambda: len(ledger_reads) == 1, "the ledger read on the return")


def test_the_card_lists_measured_apps_against_budget_and_the_rest_with_reasons(
    authed_page: Page, base_url: str
) -> None:
    """Every ledger state on one page load: a failed read, an unreadable
    ledger, a missing one, then a readable one with every kind of row.
    Last, a registry answer landing on the hidden page."""
    _fake_clock.install(authed_page)
    answer = _open_apps(authed_page, base_url, {"detail": "boom"}, status=500)

    # A failed read with nothing good to show is unknown, never green.
    _assert_unknown(authed_page, "Could not read the performance results.")

    # An unreadable ledger says so, and is still unknown.
    answer.update(perf={"ledger": "unreadable", "apps": {}, "rows": {}}, status=200)
    authed_page.locator("#perfState .async-retry").click()
    _assert_unknown(authed_page, "The performance ledger cannot be read.")

    # A missing ledger lists every app as never measured.
    answer.update(perf={"ledger": "missing", "apps": {}, "rows": {}})
    authed_page.locator("#perfState .async-retry").click()
    expect(authed_page.locator("#perfNone")).to_be_visible(timeout=5_000)
    expect(authed_page.locator("#perfSummaryMeta")).to_have_text("None measured")
    expect(authed_page.locator("#perfMeasured li")).to_have_count(0)
    expect(_reason(authed_page, "never")).to_contain_text("Good App")
    expect(authed_page.locator(".apps-perf-card")).not_to_contain_text("Within budget")

    # A readable ledger: re-opening the tab re-reads it.
    answer.update(perf=_perf_ok())
    authed_page.locator("#tabClaude").click()
    authed_page.locator("#tabApps").click()
    card = authed_page.locator(".apps-perf-card")
    expect(card.locator(".perf-intro")).to_contain_text("throttled phone-profile page load")
    expect(authed_page.locator("#perfSummaryMeta")).to_have_text("3 measured · 1 over budget", timeout=5_000)
    expect(authed_page.locator("#perfNone")).to_be_hidden()

    # Only measured apps are rows: a pass seeded from an issue log counts, the rest do not.
    expect(authed_page.locator("#perfMeasured li.perf-row")).to_have_count(3)

    good = _row(authed_page, "good-tray")
    expect(good).not_to_have_attribute("data-over", "true")
    expect(good).to_contain_text("Within budget")
    expect(good).to_contain_text("cold 1.8 s / 2.5 s")
    expect(good).to_contain_text("warm 600 ms / 1.5 s")
    expect(good).to_contain_text("transfer 410 KB / 800 KB")
    expect(good.locator("svg.perf-spark")).to_have_attribute(
        "aria-label", "Cold ready, last 3 runs: 2.1 s to 1.8 s")
    expect(good.locator("a.perf-issues")).to_have_count(0)

    slow = _row(authed_page, "slow-web")
    expect(slow).to_have_attribute("data-over", "true")
    expect(slow.locator(".chip")).to_have_text("Over budget")
    expect(slow).to_contain_text("cold ready")
    expect(slow.locator(".perf-num.is-over")).to_have_text("cold 3.1 s / 2.5 s")
    link = slow.locator("a.perf-issues")
    expect(link).to_have_attribute(
        "href", "https://github.com/example/slow-app/issues?q=is%3Aissue+is%3Aopen+label%3Aperf-review")
    expect(link).to_have_attribute("target", "_blank")

    # A pass seeded from an issue log has no numbers and says so.
    seeded = _row(authed_page, "seeded-web")
    expect(seeded).to_contain_text("Passed, no timings yet")
    expect(seeded.locator(".perf-num")).to_have_count(0)

    # Everything else is listed once, under why it has no result; none reads as a pass.
    expect(_reason(authed_page, "never")).to_have_text("Never measuredNever Run App")
    expect(_reason(authed_page, "blank-run")).to_have_text("Last run measured nothingPartial App")
    expect(_reason(authed_page, "unrecognised")).to_have_text("Result not recognisedOdd App")
    expect(_reason(authed_page, "tunnel")).to_have_text("A tunnel, not a web app of its ownGood Tunnel")
    for name in ("Never Run App", "Partial App", "Odd App", "Good Tunnel"):
        expect(authed_page.locator("#perfMeasured")).not_to_contain_text(name)

    # No app row carries a result of its own.
    expect(authed_page.locator("#appsList .action-row").first).to_be_visible()
    expect(authed_page.locator("#appsList, #registeredTraysList").locator(
        '[class*="perf"], a[href*="perf-review"]')).to_have_count(0)

    _assert_no_ledger_read_while_hidden(authed_page)


@pytest.mark.iphone
def test_the_card_fits_the_phone_width(authed_page: Page, base_url: str) -> None:
    _open_apps(authed_page, base_url, _perf_ok())
    row = _row(authed_page, "slow-web")
    expect(row).to_be_visible(timeout=5_000)
    fits = stable_eval(
        authed_page.locator(".apps-perf-card"),
        "el => el.scrollWidth <= el.clientWidth + 1 && "
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth",
    )
    assert fits is True
    # The issues link keeps the 44px touch floor.
    height = stable_eval(row.locator("a.perf-issues"), "el => el.getBoundingClientRect().height")
    assert height is not None and height >= 44
