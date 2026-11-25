"""The Board's Refresh busy state and toasts, asserted from a Board tab test.

Not a test module: it rides the setup of an existing Board test whose Backlog
lane is empty, so the suite's node count does not grow for it.
"""

from __future__ import annotations

import json as _json
import re

from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import flush_requests, wait_until


def assert_refresh_buttons_share_one_busy_state_and_toast(page: Page) -> None:
    """With the Backlog lane empty, its Refresh and the header's spin and ignore
    taps together while one refresh runs; a toast says it started and another
    how it ended."""
    held: list = []
    pattern = re.compile(r".*/api/board/github/refresh$")
    page.route(pattern, lambda route: held.append(route))
    header = page.locator("#boardRefresh")
    lane = page.locator('.board-empty[data-col="backlog"] .empty-state-action')
    toast = page.locator("#toast")

    def reply(error):
        held[-1].fulfill(status=200, content_type="application/json",
                         body=_json.dumps({"fetched_at": None, "error": error}))

    def both_busy(busy):
        for button in (header, lane):
            check = expect(button).to_have_attribute if busy else expect(button).not_to_have_attribute
            check("aria-busy", "true")

    header.click()
    wait_until(page, lambda: len(held) == 1, "the refresh POST")
    both_busy(True)
    expect(toast).to_have_text("Refreshing GitHub…")
    # The busy lane button takes no real tap: dispatch one at each, neither may post.
    lane.dispatch_event("click")
    header.dispatch_event("click")
    flush_requests(page)
    assert len(held) == 1, f"a tap during a refresh posted again: {len(held)}"
    reply(None)
    expect(toast).to_have_text("Up to date")
    both_busy(False)

    lane.click()
    wait_until(page, lambda: len(held) == 2, "the lane Refresh's POST")
    both_busy(True)
    reply("gh is down")
    expect(toast).to_have_text("GitHub refresh failed: gh is down")
    both_busy(False)
    page.unroute(pattern)
