"""Smoke + regression coverage for the Apps-tab Running apps panel (issue #35).

The panel lists apps spawned from the launcher, binds each to its port,
and offers tap-to-open over Tailscale plus a per-app Stop. A real
Streamlit launch is too heavy + flaky for the smoke suite, so the
running-apps API is route-mocked here; real-spawn integration is covered
by the issue's manual validation. Runs against the live tray (or the
autoboot disposable webapp); skips if neither is up.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock

pytestmark = pytest.mark.smoke


def _navigate(page: Page, base_url: str) -> list[str]:
    """Open the SPA, return the list that collects uncaught JS errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#runningAppsList", state="attached", timeout=5_000)
    return errors


def _two_row_payload() -> dict:
    return {
        "running": [
            {
                "app_id": "voice-transcriber-webapp",
                "name": "Voice Transcriber",
                "kind": "webapp",
                "pid": 12345,
                "started_at": 1736179200,
                "port": 8501,
                "url": "https://pc.example-tailnet.ts.net:8501/",
                "alive": True,
            },
            {
                "app_id": "photo-ocr-streamlit",
                "name": "Photo OCR",
                "kind": "streamlit",
                "pid": 23456,
                "started_at": 1736179200,
                "port": None,
                "url": None,
                "alive": True,
            },
        ]
    }


def test_section_renders_empty(authed_page: Page, base_url: str) -> None:
    authed_page.route(
        "**/api/apps/running",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"running": []}),
        ),
    )
    errors = _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    expect(authed_page.locator("#runningAppsList")).to_be_attached()
    expect(authed_page.locator("#runningAppsEmpty")).to_be_visible()
    # #1437: one line, and the "launched here" footer only while empty.
    expect(authed_page.locator("#runningAppsEmpty .empty-state-message")).to_have_text(
        "Nothing launched from here is running.")
    expect(authed_page.locator("#runningAppsNote")).to_be_visible()
    authed_page.wait_for_timeout(300)
    assert errors == [], "JS errors:\n  - " + "\n  - ".join(errors)


def test_section_renders_rows(authed_page: Page, base_url: str) -> None:
    authed_page.route(
        "**/api/apps/running",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_two_row_payload()),
        ),
    )
    _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    rows = authed_page.locator("#runningAppsList li.action-row")
    expect(rows).to_have_count(2, timeout=5_000)

    # Row 0 has a URL — Open enabled; row 1 has none — Open disabled.
    row0, row1 = rows.nth(0), rows.nth(1)
    # The row itself is Open (#1129, an action-row): enabled with a URL.
    # Since #1437 the kind is the avatar's glyph, which wears the alive
    # badge, and the context line is "up … · :port"; the pid moved to the
    # menu's header line.
    expect(row0.locator(".action-row-title")).to_have_text("Voice Transcriber")
    expect(row0.locator(".action-row-meta")).to_have_text(re.compile(r"^up .+ · :8501$"))
    expect(row0.locator(".action-open")).to_be_enabled()
    expect(row0.locator(".action-open .avatar")).to_have_attribute("data-badge", "alive")
    expect(row0.locator(".avatar use")).to_have_attribute("href", "#i-globe")
    expect(row1.locator(".avatar use")).to_have_attribute("href", "#i-gauge")
    row0.locator(".action-row-kebab").click()
    expect(row0.locator(".row-menu-head")).to_have_text("pid 12345")
    authed_page.keyboard.press("Escape")

    expect(row1.locator(".action-row-title")).to_have_text("Photo OCR")
    expect(row1.locator(".action-row-meta")).to_contain_text("binding")
    expect(row1.locator(".action-open")).to_be_disabled()
    # Something is running, so the empty state and its footer are gone.
    expect(authed_page.locator("#runningAppsEmpty")).to_be_hidden()
    expect(authed_page.locator("#runningAppsNote")).to_be_hidden()


def test_open_button_opens_new_tab(authed_page: Page, base_url: str) -> None:
    target = "https://example.invalid/"
    payload = _two_row_payload()
    payload["running"] = [payload["running"][0]]
    payload["running"][0]["url"] = target

    authed_page.route(
        "**/api/apps/running",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        ),
    )
    # Capture window.open before the SPA loads — don't actually navigate.
    authed_page.add_init_script(
        "window.__opened = [];"
        "window.open = function (u) { window.__opened.push(u); return null; };"
    )
    _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    open_btn = authed_page.locator("#runningAppsList li.action-row .action-open")
    expect(open_btn).to_be_enabled(timeout=5_000)
    open_btn.click()

    opened = authed_page.evaluate("window.__opened")
    assert opened == [target], f"window.open called with {opened!r}, expected [{target!r}]"


def test_stop_confirms_and_posts(authed_page: Page, base_url: str) -> None:
    stop_calls: list[str] = []

    payload = _two_row_payload()
    payload["running"] = [payload["running"][0]]  # one row

    def _running_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload if not stop_calls else {"running": []}),
        )

    def _stop_handler(route):
        stop_calls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"stopped": 12345}),
        )

    authed_page.route("**/api/apps/running", _running_handler)
    authed_page.route(
        "**/api/apps/*/instances/*/stop", _stop_handler
    )
    # The vendored confirm sheet asks, never native confirm() (#1437).
    native: list = []
    authed_page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))

    _navigate(authed_page, base_url)
    authed_page.locator("#tabApps").click()

    rows = authed_page.locator("#runningAppsList li.action-row")
    expect(rows).to_have_count(1, timeout=5_000)
    # Stop is the ⋮ menu's last, danger item now (#1129), still confirmed.
    rows.first.locator(".action-row-kebab").click()
    rows.first.locator(".action-stop-close").click()
    confirm = authed_page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    expect(confirm.locator("#confirmDialogTitle")).to_have_text("Stop Voice Transcriber?")
    # The modal contract (#545): a header ✕ and one full-width action.
    expect(confirm.locator(".detail-header .detail-close")).to_have_count(1)
    expect(confirm.locator(".detail-actions button")).to_have_count(1)
    confirm.locator("#confirmDialogOk").click()

    expect(rows).to_have_count(0, timeout=5_000)
    assert native == [], f"a native dialog fired: {native!r}"
    assert len(stop_calls) == 1, f"expected one stop POST, got {stop_calls!r}"
    assert stop_calls[0].endswith(
        "/api/apps/voice-transcriber-webapp/instances/12345/stop"
    ), f"stop POST hit the wrong path: {stop_calls[0]}"


def test_polling_pauses_on_other_tab(authed_page: Page, base_url: str) -> None:
    calls: list[float] = []

    def _handler(route):
        calls.append(0.0)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"running": []}),
        )

    _fake_clock.install(authed_page)
    authed_page.route("**/api/apps/running", _handler)
    _navigate(authed_page, base_url)
    # The poll is armed once boot's first fetch settles: wait for it, so the
    # jump below passes a poll that exists rather than one boot hasn't made yet.
    _fake_clock.wait_for_interval(authed_page, 4_000)  # RUNNING_APPS_POLL_MS

    # Boot lands on the Claude Code tab — fetchRunningApps self-gates, so
    # no /api/apps/running request should fire there. Jump past one poll.
    _fake_clock.advance(authed_page, 5_000)
    assert calls == [], f"polled while Apps tab hidden: {len(calls)} call(s)"

    # Switch to Apps tab → polling resumes (tab-click triggers one fetch).
    with authed_page.expect_request("**/api/apps/running"):
        authed_page.locator("#tabApps").click()
    _fake_clock.advance(authed_page, 0)
    after_open = len(calls)
    assert after_open >= 1, "no /api/apps/running request after opening Apps tab"

    # Switch away again → polling pauses; count must stop climbing.
    authed_page.locator("#tabClaude").click()
    paused_at = len(calls)
    _fake_clock.advance(authed_page, 5_000)
    assert len(calls) == paused_at, (
        f"polling kept firing after leaving the Apps tab: "
        f"{len(calls) - paused_at} extra call(s)"
    )
