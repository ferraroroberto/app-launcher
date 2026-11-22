"""The build readout's restart sheet: the session-host restart asks first.

``/api/restart`` is route-mocked before ``goto()``, so the server never starts
a restart of any kind. The session-host target's warning carries a live
session count that changes between page load and the tap: the sheet must show
the count read when it is opened (not the boot-time one), post nothing on the
first tap, and post on the second only. The mocked run then fails, and the
sheet must say so instead of reloading.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import boot_retrying_static_failure, flush_requests

pytestmark = pytest.mark.smoke

_RESTART = re.compile(r".*/api/restart$")


def test_the_session_host_restart_shows_the_live_count_and_runs_only_on_confirm(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    live = {"count": 2, "last": None}
    posts: list = []

    def handle(route) -> None:
        if route.request.method == "POST":
            posts.append(json.loads(route.request.post_data or "{}"))
            live["last"] = {"id": "r1", "target": "session-host", "state": "failed",
                            "exit_code": 1, "log_tail": ["reclaim failed"]}
            route.fulfill(status=202, content_type="application/json",
                          body=json.dumps({"restart_id": "r1", "target": "session-host"}))
            return
        warning = f"This ends {live['count']} live terminal sessions on this PC, the chief included."
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "targets": [
                {"id": "app", "label": "Restart app", "warning": ""},
                {"id": "session-host", "label": "Restart session-host", "warning": warning},
            ],
            "last": live["last"],
        }))

    page.route(_RESTART, handle)
    boot_retrying_static_failure(
        page, lambda: page.goto(f"{base_url}/", wait_until="domcontentloaded")
    )
    readout = page.locator("#buildReadout")
    expect(readout).to_have_attribute("role", "button")

    live["count"] = 5
    with page.expect_response(_RESTART):
        readout.click()
    sheet = page.locator('dialog[data-surface="app.restart-sheet"]')
    expect(sheet).to_be_visible()
    sheet.locator("select").select_option("session-host")
    flush_requests(page)

    confirm = sheet.locator(".detail-actions button")
    confirm.click()
    expect(sheet.locator(".restart-sheet-note")).to_contain_text("ends 5 live terminal sessions")
    expect(confirm).to_have_text("Restart anyway")
    assert posts == [], "the first tap only warns; nothing may be posted yet"

    confirm.click()
    expect(sheet.locator(".restart-sheet-note")).to_contain_text("Restart failed (exit 1)")
    expect(sheet.locator(".restart-sheet-log")).to_contain_text("reclaim failed")
    assert posts == [{"target": "session-host"}]
