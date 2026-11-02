"""Opening the Board survives a tap the browser never delivered (#2079).

Under full-suite load a WebKit tap on the Board tab came back done while no
``click`` reached the tab: the Code tab stayed selected and ``#paneBoard``
hidden, so the next ``to_be_visible`` waited out its 5 s. The app has no part
in it; nothing switched the tab back. These tests drop the click at the window
before the page sees it, the same state the failures left, and pin what the
shared tab helper does with it: re-tap once and say so, fail when no tap ever
lands, and fail as the app's fault when a click lands but the pane stays hidden.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._support import _mock_board, _open_board

pytestmark = pytest.mark.smoke

# Registered at document start, so it runs before every listener the app or the
# tab helper adds. "window" drops the tap before anything sees it (a lost
# input); "document" lets the window listeners see it and stops it there, short
# of the nav's own handler (a tap that landed but switched nothing).
_DROP_TAPS_JS = """(() => {
  let left = %(count)d;
  %(where)s.addEventListener('click', (ev) => {
    if (!ev.isTrusted || left === 0) return;
    if (!(ev.target.closest && ev.target.closest('#tabBoard'))) return;
    left -= 1;
    ev.preventDefault();
    ev.stopImmediatePropagation();
  }, true);
})();"""


def _drop_taps(page: Page, count: int, where: str = "window") -> None:
    page.add_init_script(_DROP_TAPS_JS % {"count": count, "where": where})


@pytest.mark.iphone
def test_a_lost_tap_on_the_board_tab_is_sent_again_and_named(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page)
    _drop_taps(authed_page, 1)
    with pytest.warns(UserWarning, match=r"e2e input lost: tap on #tabBoard never reached it"):
        _open_board(authed_page, base_url)
    expect(authed_page.locator("#tabBoard")).to_have_attribute("aria-selected", "true")


@pytest.mark.iphone
def test_a_board_tab_that_no_tap_reaches_fails_naming_the_lost_input(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page)
    _drop_taps(authed_page, -1)
    with pytest.warns(UserWarning), pytest.raises(AssertionError, match="two taps on #tabBoard never reached it"):
        _open_board(authed_page, base_url)


@pytest.mark.iphone
def test_a_tap_that_lands_but_switches_nothing_fails_as_the_app(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page)
    _drop_taps(authed_page, -1, where="document")
    with pytest.raises(AssertionError, match="a click reached #tabBoard but #paneBoard stayed hidden"):
        _open_board(authed_page, base_url)
