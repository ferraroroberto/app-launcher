"""One open phone page keeps one read in flight per shape, and a hidden tab polls nothing.

The session list, the Board and the open Chat transcript are each held open at
the route while every caller that could start another read fires: poll ticks
on the fake clock, tab taps and foreground returns. A counter wrapped around
``fetch`` records the most requests of each shape that were out at once.

Hermetic: every read is route-mocked before ``goto()``, with fictional rows.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.board._support import _mock_board
from tests.e2e.chat._support import (
    _mock_sessions_list,
    _open_chat,
    _row,
    _tick,
    _Transcript,
    _turn,
)
from tests.e2e.fixtures.waits import flush_requests

pytestmark = pytest.mark.smoke

# Counts each shape's requests in flight and the most there ever were at once.
_COUNT_FETCHES = """(() => {
  const real = window.fetch;
  const shape = (url) => {
    const path = new URL(url, location.href).pathname;
    if (path === '/api/claude-code/sessions') return 'sessions';
    if (path === '/api/board') return 'board';
    if (/\\/transcript$/.test(path)) return 'transcript';
    return path.startsWith('/api/') ? 'other' : null;
  };
  window.__flight = {};
  window.__peak = {};
  window.__seen = {};
  window.fetch = function (input, init) {
    const key = shape(typeof input === 'string' ? input : input.url);
    if (!key) return real.call(this, input, init);
    window.__flight[key] = (window.__flight[key] || 0) + 1;
    window.__seen[key] = (window.__seen[key] || 0) + 1;
    window.__peak[key] = Math.max(window.__peak[key] || 0, window.__flight[key]);
    return real.call(this, input, init).finally(() => { window.__flight[key] -= 1; });
  };
})();"""


class _Held:
    """Passes a route on to the mock under it, or holds it until ``release``."""

    def __init__(self, page: Page, pattern: str) -> None:
        self.hold = False
        self.held: list = []
        page.route(re.compile(pattern), self._handle)

    def _handle(self, route) -> None:
        if self.hold:
            self.held.append(route)
        else:
            route.fallback()

    def release(self) -> None:
        self.hold = False
        held, self.held = self.held, []
        for route in held:
            route.fallback()


def _set_hidden(page: Page, hidden: bool) -> None:
    page.evaluate(
        """(hidden) => {
          Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
          Object.defineProperty(document, 'visibilityState', {
            configurable: true, get: () => hidden ? 'hidden' : 'visible' });
          document.dispatchEvent(new Event('visibilitychange'));
        }""",
        hidden,
    )


def _boot(page: Page, base_url: str) -> _Transcript:
    page.set_viewport_size({"width": 390, "height": 844})
    _mock_sessions_list(page)
    _mock_board(page)
    tr = _Transcript(page)
    tr.append(_turn("user", "check the poll", 100), _turn("assistant", "looking", 200))
    _fake_clock.install(page)
    page.add_init_script(_COUNT_FETCHES)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(_row(page)).to_be_visible()
    _fake_clock.wait_for_interval(page, 5_000, "fetchSessions")
    return tr


def _reset_peak(page: Page, key: str) -> None:
    page.evaluate("(k) => { window.__peak[k] = window.__flight[k] || 0; }", key)


def _peak(page: Page, key: str) -> int:
    return page.evaluate("(k) => window.__peak[k] || 0", key)


def _seen(page: Page) -> dict:
    return page.evaluate("() => Object.assign({}, window.__seen)")


def _pile_on(page: Page, period_ms: int) -> None:
    """Every caller that could start a second read while one is held."""
    for _ in range(3):
        _fake_clock.advance(page, period_ms)
        _set_hidden(page, False)
    flush_requests(page)


def _open_board(page: Page) -> None:
    """Tap the Board and wait for its first read."""
    with page.expect_response(lambda r: r.url.endswith("/api/board")):
        page.locator("#tabBoard").click()
    flush_requests(page)


def _hidden_then_back(page: Page, period_ms: int, shapes: tuple[str, ...]) -> None:
    """A minute hidden asks for nothing; coming back reads each shape once."""
    _set_hidden(page, True)
    before = _seen(page)
    for _ in range(60_000 // period_ms):
        _fake_clock.advance(page, period_ms)
    assert _seen(page) == before, f"polled while hidden: {before} -> {_seen(page)}"
    _set_hidden(page, False)
    flush_requests(page)
    after = _seen(page)
    for shape in shapes:
        assert after.get(shape, 0) - before.get(shape, 0) == 1, (shape, before, after)


@pytest.mark.iphone
def test_one_request_per_shape_in_flight_and_none_while_hidden(authed_page: Page, base_url: str) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    # Registered after the mocks, so each sees a request first.
    board = _Held(page, r".*/api/board(?:\?.*)?$")
    sessions = _Held(page, r".*/api/claude-code/sessions$")

    # The session list: ticks and foreground returns while one read is held.
    _reset_peak(page, "sessions")
    sessions.hold = True
    _pile_on(page, 5_000)
    assert sessions.held, "no session-list read started"
    assert _peak(page, "sessions") == 1
    sessions.release()

    # The Board: taps on its tab, ticks and foreground returns.
    _open_board(page)
    _reset_peak(page, "board")
    board.hold = True
    page.locator("#tabClaude").click()
    page.locator("#tabBoard").click()
    _pile_on(page, 5_000)
    assert board.held, "no Board read started"
    assert _peak(page, "board") == 1
    board.release()
    flush_requests(page)
    _hidden_then_back(page, 5_000, ("sessions", "board"))

    # The open Chat transcript.
    page.locator("#tabClaude").click()
    _open_chat(page)
    _tick(page, tr)
    _reset_peak(page, "transcript")
    tr.hold = True
    _pile_on(page, 3_000)
    assert tr.held, "no transcript read started"
    assert _peak(page, "transcript") == 1
    tr.release()
    flush_requests(page)
    _hidden_then_back(page, 3_000, ("transcript",))
