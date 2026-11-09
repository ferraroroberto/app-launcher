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
# of the nav's own handler (a tap that landed but switched nothing). The
# scenario is read from ``window.__e2eDropTaps``, which a later init script
# sets, so one page runs every scenario: init scripts run in the order added.
_DROP_TAPS_JS = """(() => {
  let left = null;
  for (const where of [window, document]) {
    where.addEventListener('click', (ev) => {
      const cfg = window.__e2eDropTaps;
      if (!cfg || (cfg.where === 'window') !== (where === window)) return;
      if (left === null) left = cfg.count;
      if (!ev.isTrusted || left === 0) return;
      if (!(ev.target.closest && ev.target.closest('#tabBoard'))) return;
      left -= 1;
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }, true);
  }
})();"""


def _drop_taps(page: Page, count: int, where: str = "window") -> None:
    page.add_init_script(f"window.__e2eDropTaps = {{count: {count}, where: '{where}'}};")


@pytest.mark.iphone
def test_the_board_tab_helper_resends_a_lost_tap_and_names_each_failure(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page)
    authed_page.add_init_script(_DROP_TAPS_JS)

    # The failing scenarios run first: a tap that switches the tab would
    # make the Board the remembered tab for the next load.
    # A tap that lands but switches nothing fails as the app's fault.
    _drop_taps(authed_page, -1, where="document")
    with pytest.raises(AssertionError, match="a click reached #tabBoard but #paneBoard stayed hidden"):
        _open_board(authed_page, base_url)

    # A tab that no tap reaches fails naming the lost input.
    _drop_taps(authed_page, -1)
    with pytest.warns(UserWarning), pytest.raises(AssertionError, match="two taps on #tabBoard never reached it"):
        _open_board(authed_page, base_url)

    # One lost tap is sent again and named.
    _drop_taps(authed_page, 1)
    with pytest.warns(UserWarning, match=r"e2e input lost: tap on #tabBoard never reached it"):
        _open_board(authed_page, base_url)
    expect(authed_page.locator("#tabBoard")).to_have_attribute("aria-selected", "true")
