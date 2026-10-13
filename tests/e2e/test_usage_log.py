"""The usage log in the real page (#1524): tabs and ``data-track`` controls
produce events, the page-hidden flush reaches the app's own endpoint with the
bearer token, and what it sends is only the contract (kind, ids, session id,
device class), to the app's own origin.

Hermetic: ``/api/usage/events`` is route-mocked so the run never writes to a
real usage log. The privacy rules themselves are pinned without a browser in
``tests/test_usage_log_adoption.py`` and ``tests/js/usage_log.test.mjs``; this
is the check that the wiring in ``main.js`` / ``tabs.js`` / ``index.html``
actually connects them.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import flush_requests, wait_until

pytestmark = pytest.mark.smoke

_USAGE = re.compile(r".*/api/usage/events$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_ENVELOPE = {"session_id", "device_class", "os_class", "events"}


def test_views_and_taps_reach_the_own_origin_with_the_contract_only(
    authed_page: Page, base_url: str, auth_token: str,
) -> None:
    page = authed_page
    posts: list[dict] = []
    foreign: list[str] = []
    origin = urlparse(base_url).netloc

    def record(route) -> None:
        req = route.request
        posts.append({"headers": req.headers, "body": json.loads(req.post_data or "{}")})
        route.fulfill(status=200, content_type="application/json",
                      body='{"accepted":0,"rejected":0}')

    page.route(_USAGE, record)
    page.on("request", lambda r: foreign.append(r.url)
            if r.method == "POST" and urlparse(r.url).netloc != origin else None)

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    page.locator("#tabJobs").click()
    page.locator("#paneJobs .settings-open-btn").click()
    # The page goes hidden: the client flushes with a keepalive fetch carrying
    # the bearer header (a beacon cannot), and replaces nothing global.
    page.evaluate(
        "Object.defineProperty(document, 'visibilityState',"
        " { value: 'hidden', configurable: true });"
        " document.dispatchEvent(new Event('visibilitychange'));"
    )
    wait_until(page, lambda: any(
        e.get("kind") == "session_end" for p in posts for e in p["body"].get("events", [])
    ), "the page-hidden flush")
    flush_requests(page)

    events = [e for p in posts for e in p["body"]["events"]]
    seen = [(e["kind"], e.get("id"), e.get("view")) for e in events]
    assert ("view", "apps", None) in seen
    assert ("view", "jobs", None) in seen
    assert ("view", "settings", None) in seen
    assert ("tap", "settings-open", "jobs") in seen
    assert seen[-1][0] == "session_end"

    for p in posts:
        assert set(p["body"]) == _ENVELOPE
        assert p["body"]["device_class"] in {"phone", "tablet", "desktop"}
        if auth_token:
            assert p["headers"].get("authorization") == f"Bearer {auth_token}"
    for e in events:
        assert set(e) <= {"kind", "id", "view"}
        assert e["kind"] in {"session_start", "view", "tap", "session_end"}
        for field in ("id", "view"):
            if field in e:
                assert _ID.match(e[field]), e
    assert foreign == [], f"a POST left the app's own origin: {foreign}"
    for api in ("window.fetch", "navigator.sendBeacon"):
        assert page.evaluate(f"{api}.toString().includes('[native code]')"), f"{api} was wrapped"
    expect(page.locator("#paneSettings")).to_be_visible()
