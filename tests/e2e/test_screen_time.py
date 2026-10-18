"""Screen time (#1678): Settings › Activity › Screen time.

The row reads today's total; its sheet shows a Today / 7 days / 30 days
period, the total, daily bars split by device class, the device totals (the
chart's legend) and the time per section. Contract:

  * the row sits in its own Activity group and reads today's total;
  * the sheet follows the modal contract (#545): a ✕ and one full-width
    primary, Send to task-os (step 2/2);
  * Send posts once, toasts the outcome, and the line above it says when
    the last send worked, why the newest one failed, or that the export is
    not set up;
  * each period draws its own report: Today without a chart, a week with
    seven bars and seven labels, a month with thirty bars and four labels;
  * a bar's segments are the devices' minutes, and it names its exact
    value; the device fills differ by pattern as well as tone;
  * no data and a failed read are their own states, never a blank sheet;
  * nothing spills sideways at 390px or 320px, in either theme.

Hermetic: ``/api/usage/time`` and ``/api/screen-time/export`` are answered
by routes set before ``goto()`` with synthetic data; the real usage log and
task-os are never touched.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.conftest import open_settings, open_settings_sheet, stable_eval, wait_until

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_USAGE_RE = re.compile(r".*/api/usage/time\?.*")
_EXPORT_RE = re.compile(r".*/api/screen-time/export$")
_TODAY = {"desktop": 60.0, "phone": 25.0}
_VIEWS = {"claude": 50.0, "board": 20.0, "settings": 5.0, "(none)": 10.0}


def _day_row(day: date, index: int) -> dict:
    """Synthetic minutes: today is _TODAY, earlier days vary by index."""
    if index == 0:
        by_device = dict(_TODAY)
    else:
        by_device = {"desktop": float(10 * (index % 5)), "phone": float(5 * (index % 3))}
        if index == 3:
            by_device["tablet"] = 12.0
    by_device = {k: v for k, v in by_device.items() if v > 0}
    return {"day": day.isoformat(), "minutes": sum(by_device.values()),
            "by_device": by_device, "by_view": {}}


def _report(days: int, by: str) -> dict:
    today = date.today()
    series = [_day_row(today - timedelta(days=i), i) for i in range(days)][::-1]
    total = sum(row["minutes"] for row in series)
    window_device: dict[str, float] = {}
    for row in series:
        for k, v in row["by_device"].items():
            window_device[k] = window_device.get(k, 0.0) + v
    scale = total / sum(_VIEWS.values())
    report = {
        "days": days, "total_minutes": total, "by": by, "log_exists": True,
        "by_device": window_device,
        "by_view": {k: round(v * scale, 1) for k, v in _VIEWS.items()},
        "days_series": series,
    }
    dropped = {"day": ("by_device", "by_view"), "device": ("by_view",),
               "view": ("by_device",)}[by]
    for key in dropped:
        del report[key]
        for row in report["days_series"]:
            del row[key]
    return report


def _route_usage(page: Page, *, status: int = 200, empty: bool = False) -> list[str]:
    seen: list[str] = []

    def handle(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        days, by = int(query["days"][0]), query["by"][0]
        seen.append(f"{days}:{by}")
        if status != 200:
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps({"detail": "usage log unavailable"}))
            return
        body = _report(days, by)
        if empty:
            body.update(total_minutes=0, log_exists=False)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(_USAGE_RE, handle)
    return seen


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _route_export(page: Page, status: dict, post: list | None = None) -> list[str]:
    """GET answers ``status`` and POST ``post``'s ``[status, body]``; both
    are mutable, so a test changes them between taps. Returns the methods."""
    seen: list[str] = []

    def handle(route: Route) -> None:
        seen.append(route.request.method)
        code, body = (200, status) if route.request.method == "GET" else post
        route.fulfill(status=code, content_type="application/json", body=json.dumps(body))

    page.route(_EXPORT_RE, handle)
    return seen


def _no_sideways_spill(page: Page) -> None:
    spill = stable_eval(page.locator("#screenTimeSheet .detail-card"), """card => {
      const edge = card.getBoundingClientRect().right;
      const past = Array.from(card.querySelectorAll('*'))
        .filter((el) => el.getBoundingClientRect().right > edge + 0.5)
        .map((el) => el.className);
      return { spill: card.scrollWidth - card.clientWidth, past: past };
    }""")
    assert spill["spill"] <= 1 and not spill["past"], spill


def test_row_and_sheet_draw_each_period(authed_page: Page, base_url: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    seen = _route_usage(page)
    _route_export(page, {"configured": True, "last_ok": None, "last_error": None})
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)

    # Activity is its own group, first, and the row reads today's total.
    expect(page.locator("#paneSettings .settings-overline").first).to_have_text("Activity")
    expect(page.locator('#paneSettings [data-settings-value="screen-time"]')).to_have_text("1 h 25 m")
    assert "1:day" in seen

    open_settings_sheet(page, "screenTimeSheet")
    sheet = page.locator("#screenTimeSheet")
    # Modal contract (#545): a header ✕ and one full-width primary.
    expect(sheet.locator(".detail-header .detail-close")).to_have_count(1)
    expect(sheet.locator(".detail-actions button")).to_have_count(1)
    expect(sheet.locator(".detail-actions .button-primary")).to_have_text("Send to task-os")
    expect(page.locator("#screenTimeSent")).to_have_text("Not sent to task-os yet.")

    # Today: the total, no chart, the devices and the sections.
    expect(sheet.locator('#screenTimeRange [aria-pressed="true"]')).to_have_text("Today")
    expect(page.locator("#screenTimeTotal")).to_have_text("1 h 25 m")
    expect(page.locator("#screenTimeCaption")).to_have_text("Today")
    expect(page.locator("#screenTimeChart")).to_be_hidden()
    expect(page.locator("#screenTimeDevices .screen-time-row-label")).to_have_text(["PC", "Phone"])
    expect(page.locator("#screenTimeDevices .screen-time-row-value")).to_have_text(["1 h", "25 m"])
    expect(page.locator("#screenTimeSections .screen-time-row-label")).to_have_text(
        ["Coding", "Board", "Settings", "Life OS", "Jobs", "Apps", "Other"])
    expect(page.locator("#screenTimeSections .screen-time-row-value").first).to_have_text("50 m")
    _no_sideways_spill(page)

    # A week: seven bars, one label each; a bar names its exact value.
    sheet.locator('#screenTimeRange [data-days="7"]').click()
    expect(sheet.locator('#screenTimeRange [aria-pressed="true"]')).to_have_text("7 days")
    expect(page.locator("#screenTimeCaption")).to_contain_text("Last 7 days · ")
    expect(page.locator("#screenTimeChart")).to_be_visible()
    bars = page.locator("#screenTimeChart .screen-time-bar")
    expect(bars).to_have_count(7)
    expect(page.locator("#screenTimeChart .screen-time-tick")).to_have_count(7)
    expect(bars.last).to_have_attribute("aria-label", re.compile(r": 1 h 25 m \(PC 1 h, Phone 25 m\)$"))
    expect(bars.last.locator(".screen-time-seg")).to_have_count(2)
    # The tablet day brings the third device into the legend.
    expect(page.locator("#screenTimeDevices .screen-time-row-label")).to_have_text(["PC", "Phone", "Tablet"])
    # Today is the tallest day, so its segments fill the chart: PC 60 of 85.
    heights = stable_eval(bars.last, """bar => Array.from(bar.children).map((s) => ({
      device: s.dataset.device, h: s.getBoundingClientRect().height,
      image: getComputedStyle(s).backgroundImage,
    }))""")
    assert [h["device"] for h in heights] == ["desktop", "phone"], heights
    assert abs(heights[0]["h"] - 120 * 60 / 85) <= 1.5, heights
    assert abs(heights[1]["h"] - 120 * 25 / 85) <= 1.5, heights
    # PC is solid, phone striped: the second channel is the pattern.
    assert heights[0]["image"] == "none" and "gradient" in heights[1]["image"], heights
    _no_sideways_spill(page)

    # A month: thirty bars, about four labels, the last under today.
    sheet.locator('#screenTimeRange [data-days="30"]').click()
    expect(bars).to_have_count(30)
    ticks = page.locator("#screenTimeChart .screen-time-tick")
    expect(ticks).to_have_count(4)
    expect(ticks.last).to_have_attribute("data-edge", "end")
    _no_sideways_spill(page)
    assert {"7:device", "7:view", "30:device", "30:view"} <= set(seen), seen

    # 320px phone: still nothing past the card's edge.
    page.set_viewport_size({"width": 320, "height": 700})
    _no_sideways_spill(page)

    sheet.locator(".detail-header .detail-close").click()
    expect(sheet).to_be_hidden()


def test_no_data_and_a_failed_read_are_their_own_states(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _route_usage(page, empty=True)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "screenTimeSheet")
    state = page.locator("#screenTimeState")
    expect(state).to_have_attribute("data-state", "empty")
    expect(state).to_contain_text("No screen time today yet.")
    expect(page.locator("#screenTimeReport")).to_be_hidden()
    expect(page.locator('#paneSettings [data-settings-value="screen-time"]')).to_have_text("0 m")

    # The read fails: an error with one Retry, which reads again.
    page.unroute(_USAGE_RE)
    _route_usage(page, status=503)
    page.locator('#screenTimeRange [data-days="7"]').click()
    expect(state).to_have_attribute("data-state", "error")
    expect(state.locator(".async-retry")).to_have_count(1)
    page.unroute(_USAGE_RE)
    _route_usage(page)
    state.locator(".async-retry").click()
    expect(state).to_be_hidden()
    expect(page.locator("#screenTimeChart .screen-time-bar")).to_have_count(7)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_fills_and_text_hold_in_both_themes(authed_page: Page, base_url: str, theme: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _route_usage(page)
    page.add_init_script(f"localStorage.setItem('launcher.theme', '{theme}')")
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
    open_settings_sheet(page, "screenTimeSheet")
    page.locator('#screenTimeRange [data-days="7"]').click()
    expect(page.locator("#screenTimeChart .screen-time-bar")).to_have_count(7)
    read = stable_eval(page.locator("#screenTimeSheet"), """sheet => {
      const css = (sel, prop) => getComputedStyle(sheet.querySelector(sel))[prop];
      const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
      return {
        accent: accent,
        desktop: css('.screen-time-seg[data-device="desktop"]', 'backgroundColor'),
        total: css('#screenTimeTotal', 'color'),
        fg: css('#screenTimeSheetTitle', 'color'),
      };
    }""")
    hex_to_rgb = {"#0969da": "rgb(9, 105, 218)", "#2f81f7": "rgb(47, 129, 247)"}
    expected = {"light": "#0969da", "dark": "#2f81f7"}[theme]
    # The PC fill is this theme's accent; the total is primary text.
    assert read["desktop"] == hex_to_rgb[expected] or read["accent"].startswith("oklch"), read
    assert read["total"] == read["fg"], read
    _no_sideways_spill(page)


def test_send_to_task_os_reports_each_outcome(authed_page: Page, base_url: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _route_usage(page)
    status = {"configured": True, "last_ok": None, "last_error": None}
    ok = {"at": _now_iso(), "first": "2026-10-11", "last": "2026-10-17", "days": 7,
          "total_minutes": 300, "dry_run": False, "put": 6, "deleted": 1}
    post = [200, {"result": {k: v for k, v in ok.items() if k != "at"},
                  "configured": True, "last_ok": ok, "last_error": None}]
    seen = _route_export(page, status, post)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "screenTimeSheet")
    line = page.locator("#screenTimeSent")
    send = page.locator("#screenTimeSend")
    toast = page.locator("#toast")
    expect(line).to_have_text("Not sent to task-os yet.")

    # A send that works: one POST, a toast, and the line names it.
    send.click()
    expect(toast).to_have_text("Sent 7 days to task-os")
    expect(line).to_have_text(re.compile(r"^Last sent to task-os today \d\d:\d\d\.$"))
    expect(send).to_have_text("Send to task-os")
    expect(send).to_be_enabled()
    assert seen.count("POST") == 1, seen
    _no_sideways_spill(page)

    # task-os down: an error toast, and the line shows the newer failure.
    post[:] = [503, {"detail": "task-os is not reachable"}]
    status.update(last_ok=ok, last_error={
        "at": "9999-12-31T23:59:59Z", "kind": "unreachable", "message": "task-os is not reachable"})
    send.click()
    expect(toast).to_have_text("Send failed: task-os is not reachable")
    expect(line).to_have_text(re.compile(r"^Last send failed .*: task-os is not reachable$"))
    expect(send).to_be_enabled()
    wait_until(page, lambda: seen.count("POST") == 2, "the second send")

    # Not set up: the line says so; Send stays tappable and toasts why.
    page.locator("#screenTimeSheet .detail-header .detail-close").click()
    status.update(configured=False, last_ok=None, last_error=None)
    post[:] = [409, {"detail": "set task_os_person in webapp_config.json"}]
    open_settings_sheet(page, "screenTimeSheet")
    expect(line).to_have_text("Not set up: fill the task-os keys in webapp_config.json.")
    send.click()
    expect(toast).to_have_text("Send failed: set task_os_person in webapp_config.json")
