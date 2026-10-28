"""A page load posts one timing row (#1924).

``static/perf-timing.js`` records the load once boot's fetches have settled
and sends it when the page hides. The row must carry the document legs and
name the boot request that landed last, which is the leg a slow relaunch is
read by (``docs/perf-timings.md``). The fake clock is not used: it replaces
``performance``, whose entries are what is under test.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page

_HIDE = """() => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  document.dispatchEvent(new Event('visibilitychange'));
}"""


def test_load_posts_a_row_naming_the_last_boot_request(authed_page: Page, base_url: str) -> None:
    authed_page.route(
        re.compile(r".*/api/perf/timings$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"accepted": 1}'),
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # Boot's fan-out has settled once the network is idle; the load row is
    # queued synchronously right after it.
    authed_page.wait_for_load_state("networkidle")
    with authed_page.expect_request(re.compile(r".*/api/perf/timings$")) as sent:
        authed_page.evaluate(_HIDE)
    batch = json.loads(sent.value.post_data or "{}")
    rows = [row for row in batch.get("rows", []) if row.get("kind") == "load"]
    assert len(rows) == 1, f"expected one load row, got {batch!r}"
    row = rows[0]
    assert row["last_path"].startswith("/api/"), row
    assert "?" not in row["last_path"], row
    assert row["boot"] is not None and row["boot"] > 0, row
    assert row["ttfb"] is not None, row
    assert batch["device_class"] in {"phone", "tablet", "desktop"}
