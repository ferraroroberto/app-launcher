"""Regression pin for issue #430 (warm terminal re-open).

The bug class: every overlay close disposed the xterm + WebSocket, so
every re-open re-subscribed to the session-host, which answers a
fullscreen (re)connect with a clear-frame + winsize-toggle repaint nudge
— and a ratatui agent (Codex/Antigravity/Pi/Copilot) re-emits its ENTIRE
transcript on any winsize change (empirical probe on #430: ~65 KB for a
long conversation, on every open/close cycle). The phone watched the
whole conversation scroll through on every re-open, and the always-on PC
mirror flashed with it.

The fix keeps a warm per-session terminal cache: hiding the overlay
stashes the xterm (painted frame intact) and keeps the WS streaming;
re-opening the same session shows the same canvas and reuses the same
socket — no re-subscribe, no repaint nudge, no re-emission.

This pins the cache contract end-to-end in the SPA:
  1. Deep-link open creates exactly one WS; the xterm element is tagged.
  2. hideTerminal() hides the overlay but leaves the tagged element in
     the DOM and the WS open.
  3. openTerminal() for the same sid re-shows the SAME element (tag
     intact), opens NO new WS, and input still reaches the PTY log over
     the original socket.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_WS_PROBE = """
(() => {
  const orig = window.WebSocket;
  const instances = [];
  function Wrapped(...args) {
    const ws = new orig(...args);
    instances.push(ws);
    return ws;
  }
  Wrapped.prototype = orig.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) {
    Wrapped[k] = orig[k];
  }
  window.WebSocket = Wrapped;
  window.__wsInstances = instances;
})();
"""

# The SPA loads its modules cache-busted (`terminal.js?v=<asset_hash>`).
# A bare import('/static/terminal.js') would evaluate a SECOND module
# instance with its own empty warm cache and its own state — silently
# testing a parallel universe. Resolve the page's real module URL from
# the resource timeline so the import shares the live instance.
_LIVE_MODULE = """
(name) => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/' + name + '?v='));
  return hit || ('/static/' + name);
}
"""


def test_warm_reopen_reuses_terminal_and_ws(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    sid = launched_pty_session
    authed_page.add_init_script(_WS_PROBE)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    # First open: one WS, reaching OPEN.
    authed_page.wait_for_function(
        "() => window.__wsInstances && window.__wsInstances.length === 1 "
        "&& window.__wsInstances[0].readyState === 1",
        timeout=10_000,
    )
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()

    # Tag the live xterm root so element identity survives the round-trip.
    authed_page.wait_for_selector("#terminalHost .xterm", timeout=10_000)
    authed_page.evaluate(
        "document.querySelector('#terminalHost .xterm').dataset.warm = 'tagged'"
    )

    # Close the overlay — must stash, not dispose (#430): element stays in
    # the DOM (hidden) and the WS stays OPEN.
    authed_page.evaluate(
        "async () => (await import(("
        + _LIVE_MODULE
        + ")('terminal.js'))).hideTerminal()"
    )
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()
    kept = authed_page.evaluate(
        """() => {
          const el = document.querySelector('#terminalHost .xterm');
          return {
            tag: el ? el.dataset.warm : null,
            hidden: el ? el.style.display : null,
            wsCount: window.__wsInstances.length,
            wsState: window.__wsInstances[0].readyState,
          };
        }"""
    )
    assert kept["tag"] == "tagged", (
        "hiding the overlay destroyed the xterm element — the warm cache "
        "(#430) is gone and every re-open will replay the transcript"
    )
    assert kept["hidden"] == "none", (
        f"stashed terminal element should be display:none, got "
        f"{kept['hidden']!r}"
    )
    assert kept["wsCount"] == 1 and kept["wsState"] == 1, (
        f"hiding the overlay should keep the single WS open, got "
        f"count={kept['wsCount']} state={kept['wsState']}"
    )

    # Re-open the same session: same element, same socket, no new WS.
    authed_page.evaluate(
        """async (sid) => {
          const live = """ + _LIVE_MODULE + """;
          const term = await import(live('terminal.js'));
          const { state } = await import(live('state.js'));
          const s = (state.sessions || []).find(
            (x) => x.session_id === sid
          ) || { session_id: sid, name: 'warm-reopen' };
          await term.openTerminal(s);
        }""",
        sid,
    )
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()
    reopened = authed_page.evaluate(
        """() => {
          const el = document.querySelector('#terminalHost .xterm');
          return {
            tag: el ? el.dataset.warm : null,
            display: el ? el.style.display : null,
            wsCount: window.__wsInstances.length,
            wsState: window.__wsInstances.at(-1).readyState,
          };
        }"""
    )
    assert reopened["tag"] == "tagged", (
        "re-open built a fresh xterm instead of resuming the cached one — "
        "the re-subscribe fires the server repaint nudge the cache exists "
        "to avoid"
    )
    assert reopened["display"] == "", (
        f"resumed terminal element should be shown, got "
        f"{reopened['display']!r}"
    )
    assert reopened["wsCount"] == 1 and reopened["wsState"] == 1, (
        f"re-open must reuse the original WS, got count={reopened['wsCount']} "
        f"state={reopened['wsState']}"
    )

    # The reused socket still carries real input to the PTY.
    marker = "warm-re0pen-marker-430\n"
    authed_page.evaluate(
        "(text) => window.__wsInstances[0].send("
        "JSON.stringify({ type: 'input', data: text }))",
        marker,
    )
    assert wait_for_session_log(authed_page, sid, "warm-re0pen-marker-430"), (
        f"input sent on the reused ws did not appear in "
        f"webapp/sessions/{sid}.log — the warm re-open resumed a dead socket"
    )
