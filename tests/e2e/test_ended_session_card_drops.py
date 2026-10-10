"""Regression pin for #1488: an ended session's card leaves the Code list on
the next update, with no refresh and no tab switch.

The sessions poll used to pause whenever a session view was open (so it would
not re-render the list "under" the overlay). On the wide layout the view docks
*beside* the list (#1135), so the list stayed on screen while its poll was
stopped: a session that ended while its view was docked kept its card until a
reload or a tab switch (which closes the view). The poll now pauses only when
the view covers the list. Below the wide breakpoint the view does cover it, and
closing the view refreshes the list (``hideTerminal``), which the second test
keeps pinned.

Detached rows are used because a desktop tap on a full-control row opens its
own PC window instead (#282); a detached row's Chat is the in-page view on
every device. Data is synthetic and every fetch the rows depend on is mocked
(#510); the sessions poll is driven by the fake clock (#1376).
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import flush_requests

pytestmark = pytest.mark.smoke

_KEEP = "sid-keep-1488"
_END = "sid-end-1488"


def _row_data(sid: str, title: str, alive: bool = True) -> dict:
    return {
        "session_id": sid, "kind": "remote", "agent": "claude",
        "project_dir": "E:/automation/endproj", "name": "endproj",
        "alive": alive, "started_at": 1_790_150_400.0,
        "live_title": "", "prompt_title": "", "manual_title": title,
    }


def _mock(page: Page) -> dict:
    """Mock the list and what the rows depend on; ``knobs['sessions']`` is the
    list the next poll returns, so a test ends a session by editing it."""
    knobs = {"sessions": [_row_data(_KEEP, "Keeps running"), _row_data(_END, "Ends soon")]}

    def fulfill(body):
        return lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    page.route(re.compile(r".*/api/claude-code/git-status$"), fulfill({"projects": []}))
    page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: fulfill({"sessions": knobs["sessions"]})(route),
    )
    page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/transcript(\?.*)?$"),
        fulfill({
            "available": True, "source": "native", "reason": None,
            "entries": [], "next_cursor": None, "session_id": _END,
        }),
    )
    return knobs


def _row(page: Page, sid: str):
    return page.locator(f'#sessionsList li.session-item[data-session-id="{sid}"]')


def _boot(page: Page, base_url: str) -> dict:
    _fake_clock.install(page)
    knobs = _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(_row(page, _END)).to_be_visible()
    # Boot arms the sessions poll only after its first fetch settles; a jump
    # made earlier would pass a poll that does not exist yet.
    _fake_clock.wait_for_interval(page, 5000, "fetchSessions")
    return knobs


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_docked_view_does_not_keep_an_ended_sessions_card(
    authed_page: Page, base_url: str, browser_name: str, theme: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("fine-pointer layout; the WebKit projection is an iPhone")
    page = authed_page
    page.set_viewport_size({"width": 1440, "height": 900})
    knobs = _boot(page, base_url)
    page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)

    # The session whose card must go is the one docked beside the list.
    _row(page, _END).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible()

    # It ends: first still listed but down (the host keeps it a moment), then gone.
    knobs["sessions"] = [_row_data(_KEEP, "Keeps running"), _row_data(_END, "Ends soon", alive=False)]
    _fake_clock.advance(page, 5_000)
    expect(_row(page, _END).locator('.avatar-badge[data-badge="down"]')).to_be_attached()
    knobs["sessions"] = [_row_data(_KEEP, "Keeps running")]
    _fake_clock.advance(page, 5_000)
    flush_requests(page)

    expect(_row(page, _END)).to_have_count(0)
    expect(_row(page, _KEEP)).to_be_visible()


@pytest.mark.iphone
def test_covering_view_refreshes_the_list_when_it_closes(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    knobs = _boot(page, base_url)

    _row(page, _END).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible()

    knobs["sessions"] = [_row_data(_KEEP, "Keeps running")]
    page.locator("#terminalBack").click()
    expect(page.locator("#terminalOverlay")).to_be_hidden()

    expect(_row(page, _END)).to_have_count(0)
    expect(_row(page, _KEEP)).to_be_visible()
