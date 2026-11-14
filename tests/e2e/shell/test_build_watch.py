"""A page left open across a deploy offers a reload (#2370).

The page boots on one asset hash (the ``?v=`` its modules carry) and reads
``/api/version`` again when it becomes visible. A differing ``asset_hash``
raises a Reload prompt; matching, empty or unreadable never does (unknown is
not stale). ``/api/version`` is route-mocked before ``goto()``; the booted hash
is read from the page's own entry module URL, so nothing is hard-coded, and
the clock is jumped past the 60 s throttle instead of waited out (#1376).
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.waits import boot_retrying_static_failure, flush_requests

pytestmark = pytest.mark.smoke

_PROMPT = "#buildPrompt"
_THROTTLE_MS = 61_000


def _become_visible(page: Page) -> None:
    """Jump past the throttle, then fire the visibility event and wait for the read it triggers.

    The page is hidden while the clock jumps: the 5-minute interval skips a
    hidden page, so only the event under test reads the server.
    """
    set_hidden = "(hidden) => Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })"
    page.evaluate(set_hidden, True)
    _fake_clock.advance(page, _THROTTLE_MS)
    page.evaluate(set_hidden, False)
    with page.expect_response(re.compile(r".*/api/version$")):
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    flush_requests(page)


def test_a_newer_build_raises_a_reload_and_unknown_never_does(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    current = {"status": 200, "body": {"asset_hash": "", "git_sha": "x"}}

    def handle(route) -> None:
        route.fulfill(
            status=current["status"],
            content_type="application/json",
            body=json.dumps(current["body"]),
        )

    page.route(re.compile(r".*/api/version$"), handle)
    _fake_clock.install(page)
    boot_retrying_static_failure(
        page, lambda: page.goto(f"{base_url}/", wait_until="domcontentloaded")
    )
    booted = page.evaluate(
        "() => new URL(document.querySelector('script[src*=\"main.js\"]').src).searchParams.get('v')"
    )
    assert booted, "the served page carries no ?v= stamp, so there is no build to compare"
    prompt = page.locator(_PROMPT)
    expect(prompt).to_have_count(0)

    # Current or unknown: the same build, an empty hash, an unreadable answer.
    for status, body in (
        (200, {"asset_hash": booted}),
        (200, {"asset_hash": ""}),
        (500, {"detail": "boom"}),
    ):
        current["status"], current["body"] = status, body
        _become_visible(page)
        expect(prompt).to_have_count(0)

    # A newer build raises the prompt; dismissing it holds the same build down.
    current["status"], current["body"] = 200, {"asset_hash": booted + "-newer"}
    _become_visible(page)
    expect(prompt).to_be_visible()
    expect(prompt).to_contain_text("New version available")
    prompt.get_by_role("button", name="Dismiss").click()
    expect(prompt).to_be_hidden()
    _become_visible(page)
    expect(prompt).to_be_hidden()

    # A build newer still asks again, and Reload is the user's tap, never automatic.
    current["body"] = {"asset_hash": booted + "-newest"}
    _become_visible(page)
    expect(prompt).to_be_visible()
    with page.expect_navigation():
        prompt.get_by_role("button", name="Reload").click()
