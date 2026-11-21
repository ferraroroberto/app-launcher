"""First load never waits on a slow source.

The session list and the Board paint what this device last saw before any
request goes out (``static/shared/boot-snapshot.js``), so a server whose every
read hangs still shows both lists. And a reload on the Board tab reads the
Board at boot rather than on its first 5 s poll.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._support import _board_payload, _mock_board

pytestmark = pytest.mark.smoke

_SNAPSHOT_SESSION = {
    "session_id": "s-snapshot", "kind": "pty", "agent": "claude",
    "project_dir": "E:/automation/demo", "name": "demo", "alive": True,
    "started_at": "2026-07-02T11:56:00Z", "live_title": "",
    "prompt_title": "", "manual_title": "Last known session",
}

# Every read the two lists or boot itself could wait on.
_HUNG = re.compile(
    r".*/api/(config|agents|claude-code/sessions|claude-code/git-status|board"
    r"|board/chief-plan|apps|status|version|rate-limits|jobs)(\?.*)?$")

# Notes, from inside the page, whether the first Board read came from a poll
# tick: the Board's poll is the interval whose callback names fetchBoard.
_SPY_FIRST_BOARD_READ = """(() => {
  const realInterval = window.setInterval;
  window.__inBoardPoll = false;
  window.setInterval = function (fn, ms, ...rest) {
    if (String(fn).includes('fetchBoard')) {
      const tick = fn;
      fn = function () {
        window.__inBoardPoll = true;
        try { return tick.apply(this, arguments); } finally { window.__inBoardPoll = false; }
      };
    }
    return realInterval.call(this, fn, ms, ...rest);
  };
  const realFetch = window.fetch;
  window.__firstBoardReadByPoll = null;
  window.fetch = function (input, ...rest) {
    const url = String(input && input.url ? input.url : input);
    if (/\\/api\\/board(\\?|$)/.test(url) && window.__firstBoardReadByPoll === null) {
      window.__firstBoardReadByPoll = window.__inBoardPoll;
    }
    return realFetch.call(this, input, ...rest);
  };
})();"""


def _seed(key: str, body: dict) -> str:
    """Init script writing one last-known snapshot, stamped with the page clock."""
    return (
        "localStorage.setItem(" + _json.dumps("app-launcher.snapshot.v1." + key)
        + ", JSON.stringify({at: Date.now(), body: " + _json.dumps(body) + "}));"
    )


def test_first_load_draws_last_known_lists_while_every_read_hangs(
    authed_page: Page, base_url: str
) -> None:
    held = []
    # Stubs the gh-refresh POST so nothing reaches the real server's gh.
    _mock_board(authed_page)
    authed_page.route(_HUNG, lambda route: held.append(route))
    authed_page.add_init_script("localStorage.setItem('app-launcher.tab', 'board');")
    authed_page.add_init_script(_SPY_FIRST_BOARD_READ)
    authed_page.add_init_script(_seed("sessions", {"sessions": [_SNAPSHOT_SESSION]}))
    authed_page.add_init_script(
        _seed("board", {"board": _board_payload(), "plan": {"state": "empty"}}))
    try:
        authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
        # Nothing has answered, so both lists came from their snapshots.
        expect(authed_page.locator("#boardColumns")).to_be_visible()
        expect(authed_page.locator("#boardColumns")).to_contain_text("chunk merge fix")
        expect(authed_page.locator("#boardFetchState")).to_be_hidden()
        expect(authed_page.locator('#sessionsList [data-session-id="s-snapshot"]')).to_contain_text(
            "Last known session")
        # The live Board read went out at boot, not on the first poll tick.
        authed_page.wait_for_function("() => window.__firstBoardReadByPoll !== null")
        assert authed_page.evaluate("() => window.__firstBoardReadByPoll") is False
        assert held, "the hung reads never went out"
    finally:
        for route in held:
            try:
                route.abort()
            except Exception:  # noqa: BLE001 - the page may already be gone
                pass
