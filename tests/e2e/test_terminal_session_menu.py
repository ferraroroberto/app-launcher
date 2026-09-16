"""Regression pins for #981 — the terminal bar's ⋮ session menu and the
floating Latest pill (Step 2 of #979).

The bar used to carry a permanent ✕ Kill and ↓ Jump. #981 folds Kill into a
⋮ menu next to 🔊 (Rename · Copy link · Stop and kill — the kill path itself
is pinned by ``test_stop_unify_and_terminal_kill.py``) and turns ↓ into a
pill over the terminal that only appears while scrolled up.

These pin:

* Copy link writes the launcher's own ``?session=<sid>`` deep link (the link
  main.js boots straight into the session) and closes the menu.
* Rename from the menu updates the bar title at once, not on the next poll.
* The Latest pill is hidden at the tail and within a screen of it, visible
  once scrolled further up, and a tap returns to the tail and hides it.

The overlay is opened with ``?session=`` rather than ``?terminal=``: the
latter marks a PC mirror window, and neither the menu nor the pill depends on
which, so ``?session=`` runs the same in-page overlay on both projections.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

_CLIPBOARD_MOCK = """
(() => {
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (text) => { window.__copied.push(text); },
      readText: async () => '',
    },
  });
})()
"""

# The SPA loads its modules cache-busted (`state.js?v=<asset_hash>`); a bare
# import would evaluate a second, empty module instance. Resolve the page's
# real module URL so the import shares the live state (same pattern as
# test_warm_terminal_reopen.py / test_terminal_reconnect.py).
_LIVE_STATE_SETUP = """
async () => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/state.js?v='));
  const { state } = await import(hit || '/static/state.js');
  window.__term = () => state.terminal && state.terminal.term;
}
"""

# Count frames each terminal socket delivers. The session-host opens every
# PTY stream with a clear frame that erases scrollback (`ESC[3J`,
# server.py _CLEAR_FRAME), so scrollback written before that frame lands is
# wiped — which is what a loaded gate run hit when the connect was slow.
_WS_FRAME_PROBE = """
(() => {
  const Orig = window.WebSocket;
  window.__wsFrames = 0;
  function Wrapped(...args) {
    const ws = new Orig(...args);
    ws.addEventListener('message', () => { window.__wsFrames += 1; });
    return ws;
  }
  Wrapped.prototype = Orig.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) Wrapped[k] = Orig[k];
  window.WebSocket = Wrapped;
})();
"""

# Settle past the pill's once-per-frame coalescing before reading it.
_TWO_FRAMES = (
    "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
)


def _open_overlay(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?session={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)


def test_menu_copy_link_and_rename_update_the_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    sid = launched_pty_session
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    _open_overlay(authed_page, base_url, sid)
    menu = authed_page.locator("#terminalOverlay .terminal-menu")

    authed_page.locator("#terminalMenu").click()
    expect(menu).to_be_visible()
    authed_page.get_by_role("menuitem", name="Copy session link").click()
    expect(menu).to_be_hidden()
    authed_page.wait_for_function("() => window.__copied.length === 1", timeout=5_000)
    copied = authed_page.evaluate("() => window.__copied[0]")
    expected = authed_page.evaluate(
        "(sid) => location.origin + location.pathname + '?session=' + sid", sid
    )
    assert copied == expected, f"copied {copied!r}, expected the ?session= link {expected!r}"

    authed_page.locator("#terminalMenu").click()
    authed_page.get_by_role("menuitem", name="Rename session").click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_visible()
    authed_page.locator("#sessionRenameInput").fill("Menu rename 981")
    authed_page.locator("#sessionRenameForm button[type='submit']").click()
    expect(authed_page.locator("#sessionRenameDialog")).to_be_hidden()
    # Shorter than the title poll (SESSIONS_POLL_MS), so a pass means the
    # rename callback updated the bar, not a later sessions fetch.
    expect(authed_page.locator("#terminalTitle")).to_have_text(
        "Menu rename 981", timeout=2_000
    )


def test_latest_pill_shows_only_when_scrolled_up(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.add_init_script(_WS_FRAME_PROBE)
    _open_overlay(authed_page, base_url, launched_pty_session)
    authed_page.evaluate(_LIVE_STATE_SETUP)
    authed_page.wait_for_function("() => !!window.__term()", timeout=OVERLAY_OPEN_MS)
    # Only write scrollback once the connect's clear + replay frame is in.
    authed_page.wait_for_function("() => window.__wsFrames > 0", timeout=OVERLAY_OPEN_MS)
    authed_page.evaluate(_TWO_FRAMES)
    pill = authed_page.locator("#terminalLatest")

    # Several screens of local scrollback (written into xterm, not the PTY).
    authed_page.evaluate(
        "() => new Promise((r) => window.__term().write("
        "Array.from({length: 400}, (_, i) => 'latest-pill line ' + i).join('\\r\\n')"
        " + '\\r\\n', r))"
    )
    authed_page.evaluate("() => window.__term().scrollToBottom()")
    authed_page.evaluate(_TWO_FRAMES)
    expect(pill).to_be_hidden()

    # A couple of lines up is still "at the latest output": no pill.
    authed_page.evaluate("() => window.__term().scrollLines(-2)")
    authed_page.evaluate(_TWO_FRAMES)
    assert authed_page.evaluate("() => document.getElementById('terminalLatest').hidden"), (
        "pill appeared less than a screen above the tail"
    )

    authed_page.evaluate("() => window.__term().scrollToTop()")
    expect(pill).to_be_visible()

    pill.click()
    expect(pill).to_be_hidden()
    at_tail = authed_page.evaluate(
        "() => { const b = window.__term().buffer.active; return b.viewportY === b.baseY; }"
    )
    assert at_tail, "tapping Latest did not return the terminal to its tail"
