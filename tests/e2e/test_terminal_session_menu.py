"""Regression pins for #981 — the terminal bar's ⋮ session menu and the
floating Latest pill (Step 2 of #979).

The bar used to carry a permanent ✕ Kill and ↓ Jump. #981 folds Kill into a
⋮ menu next to 🔊 (Rename · Copy link · Stop and kill — the kill path itself
is pinned by ``test_stop_unify_and_terminal_kill.py``) and turns ↓ into a
pill over the terminal that only appears while scrolled up.

These pin:

* Copy link writes the session's **provider-native** link when it has one —
  Claude's ``claude.ai/code/session_…`` remote-control URL, the same string
  the Rename / link dialog shows — and falls back to this launcher's own
  ``?session=<sid>`` deep link otherwise, with a toast that names which
  (#1096; #981 had copied the launcher link unconditionally, re-introducing
  what #879 removed — a tailnet-only URL that is dead on any other network).
* Rename from the menu updates the bar title at once, not on the next poll.
* The Latest pill is hidden at the tail and within a screen of it, visible
  once scrolled further up, and a tap returns to the tail and hides it.

The overlay is opened with ``?session=`` rather than ``?terminal=``: the
latter marks a PC mirror window, and neither the menu nor the pill depends on
which, so ``?session=`` runs the same in-page overlay on both projections.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

# What the server would hand back as `web_url` once Claude's remote-control
# card has been printed into the PTY and scanned (routers/_helpers.py). The
# token is synthetic — the shape is all this test needs, and it never goes
# near the server-side regex, which only ever parses a real transcript.
_CLAUDE_WEB_URL = "https://claude.ai/code/session_E2E1096FALLBACKPROVIDER1"

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

# Expose the page's live `state` (same cache-busted-module resolution as
# _LIVE_STATE_SETUP) so a test can empty `state.sessions` mid-overlay — the
# bare ?session= deep-link shape, where currentSession() resolves to the
# overlay's own {session_id, name} and there is no web_url to prefer.
_LIVE_STATE_HANDLE = """
async () => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/state.js?v='));
  const { state } = await import(hit || '/static/state.js');
  window.__state = state;
}
"""


def _inject_web_url(page: Page, sid: str, web_url: str) -> None:
    """Pass /api/claude-code/sessions through, stamping one row's web_url.

    A static payload would fight the overlay's own 5s title poll, which reads
    the same endpoint (terminal.js) — the rename below would be reverted by
    it. Patching the real response keeps every other field (manual_title
    included) exactly as the server computed it; only the field the stub
    child can never produce — Claude prints no remote-control card — is
    supplied, and `agent` is pinned to claude so the row is the shape this
    branch is about.
    """

    def _handler(route):
        response = route.fetch()
        body = response.json()
        for session in body.get("sessions", []):
            if session.get("session_id") == sid:
                session["agent"] = "claude"
                session["web_url"] = web_url
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    page.route(re.compile(r".*/api/claude-code/sessions$"), _handler)


def _copy_link(page: Page) -> None:
    menu = page.locator("#terminalOverlay .terminal-menu")
    page.locator("#terminalMenu").click()
    expect(menu).to_be_visible()
    page.get_by_role("menuitem", name="Copy session link").click()
    expect(menu).to_be_hidden()


def _open_overlay(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?session={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)


def test_menu_copy_link_and_rename_update_the_bar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    sid = launched_pty_session
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    _inject_web_url(authed_page, sid, _CLAUDE_WEB_URL)
    _open_overlay(authed_page, base_url, sid)

    # A Claude full-control session with its card captured: the provider link
    # wins, and the toast says so rather than folding the two outcomes.
    _copy_link(authed_page)
    authed_page.wait_for_function("() => window.__copied.length === 1", timeout=5_000)
    copied = authed_page.evaluate("() => window.__copied[0]")
    assert copied == _CLAUDE_WEB_URL, (
        f"copied {copied!r}, expected the Claude web link {_CLAUDE_WEB_URL!r}"
    )
    expect(authed_page.locator("#toast")).to_have_text(
        re.compile("Claude web link copied")
    )

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

    # Last, because it mutates the page's own state: the bare ?session=
    # deep-link shape, where the sessions list does not carry the row and
    # main.js opens the overlay with a bare {session_id, name} instead
    # (`openTerminal(found || {...})`). currentSession() then resolves to
    # that object, which has no agent and no web_url — it must land on the
    # launcher link, never on `undefined`, and the toast must flag it
    # tailnet-only rather than staying silent about the downgrade.
    #
    # Both halves are set: emptying state.sessions alone is not that shape,
    # because state.sessionView.session is the *same object* the boot lookup
    # found and would still carry web_url.
    authed_page.evaluate(_LIVE_STATE_HANDLE)
    authed_page.evaluate(
        "(sid) => { window.__state.sessions = [];"
        " window.__state.sessionView.session = {session_id: sid, name: sid}; }",
        sid,
    )
    _copy_link(authed_page)
    authed_page.wait_for_function("() => window.__copied.length === 2", timeout=5_000)
    fallback = authed_page.evaluate("() => window.__copied[1]")
    expected = authed_page.evaluate(
        "(sid) => location.origin + location.pathname + '?session=' + sid", sid
    )
    # GUARD, not proof: pre-fix code copied sessionShareUrl unconditionally,
    # so this assertion was green before the fix too (measured). It is here to
    # stop a future change breaking the fallback, not to demonstrate #1096.
    assert fallback == expected, (
        f"copied {fallback!r}, expected the ?session= link {expected!r}"
    )
    # PROOF: pre-fix this toast read "Session link copied" for both outcomes —
    # the silent fold that let the wrong link ship unnoticed for two weeks.
    expect(authed_page.locator("#toast")).to_have_text(
        re.compile(r"Launcher link copied \(tailnet only\)")
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
