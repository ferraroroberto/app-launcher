"""Regression pin for commit 142e2b4 / issue #28 (live terminal WS reconnect).

The bug: iOS aggressively suspends backgrounded PWAs; uvicorn's ping
timeout then closes the half-dead WebSocket and the overlay was left
frozen on "Disconnected." until the user manually re-opened the
session. The fix factors the ws setup into ``connectWs(t)`` and
re-runs it after non-final close codes with 1s/2s/4s/8s backoff.

This test exercises the JS half of the fix by:
  1. Wrapping ``window.WebSocket`` in an init script so the test can
     observe every WS the SPA opens (no product change).
  2. Opening the terminal via ``?terminal=<sid>`` deep-link.
  3. Force-closing the open WS from the page (code 1005). Per
     ``terminal.js:113-114`` this is the "iOS-suspend" path — same
     code reached when uvicorn's ping timeout fires.
  4. Asserting the SPA opens a fresh WS and that input sent on the
     fresh socket reaches ``webapp/sessions/<sid>.log``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Wrap WebSocket so we can see every instance the SPA constructs. Runs
# before any page script, so connectWs() in terminal.js uses the wrapped
# constructor without any code change.
_WS_PROBE = """
(() => {
  const orig = window.WebSocket;
  const instances = [];
  function Wrapped(...args) {
    const ws = new orig(...args);
    // First text frame per socket (#444): the reconnect-replay tests assert
    // the server's clear-frame preamble arrives in the same frame as the
    // ring snapshot. Registered here so no frame can slip by before the
    // SPA's own onmessage is assigned.
    ws.__firstMsg = null;
    ws.addEventListener('message', (ev) => {
      if (ws.__firstMsg === null && typeof ev.data === 'string') {
        ws.__firstMsg = ev.data.slice(0, 64);
      }
    });
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


def test_terminal_reconnects_after_ws_drop(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    sid = launched_pty_session
    authed_page.add_init_script(_WS_PROBE)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    # Wait for the first WS to reach OPEN. The deep-link path calls
    # openTerminal automatically after fetchSessions resolves.
    authed_page.wait_for_function(
        "() => window.__wsInstances && window.__wsInstances.length >= 1 "
        "&& window.__wsInstances[0].readyState === 1",
        timeout=10_000,
    )

    # Force-drop the live socket. close() with no args fires onclose with
    # code 1005, which terminal.js routes to scheduleReconnect (line 113).
    authed_page.evaluate("window.__wsInstances.at(-1).close()")

    # Reconnect budget in the SPA is 30s with 1s/2s/4s/8s backoff. Allow
    # 15s here — comfortably inside the first two backoff cycles.
    authed_page.wait_for_function(
        "() => window.__wsInstances.length >= 2 "
        "&& window.__wsInstances.at(-1).readyState === 1",
        timeout=15_000,
    )

    # Send a recognisable string on the new socket and confirm it lands
    # in the per-session log. This proves the reconnect carried real
    # I/O, not just a TCP handshake.
    marker = "rec0nn3ct-marker-32\n"
    authed_page.evaluate(
        "(text) => window.__wsInstances.at(-1).send("
        "JSON.stringify({ type: 'input', data: text }))",
        marker,
    )

    # Session log is buffered; poll for a couple of seconds.
    assert wait_for_session_log(authed_page, sid, "rec0nn3ct-marker-32"), (
        f"input sent on the reconnected ws did not appear in "
        f"webapp/sessions/{sid}.log — the reconnect handshake succeeded but "
        "the new ws isn't carrying input"
    )


# --------------------------------------------------------------- issue #444

# The SPA loads its modules cache-busted (`state.js?v=<asset_hash>`). A bare
# import('/static/state.js') would evaluate a SECOND module instance with its
# own empty state — silently testing a parallel universe. Resolve the page's
# real module URL from the resource timeline so the import shares the live
# instance (same pattern as test_warm_terminal_reopen.py).
_LIVE_MODULE = """
(name) => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/' + name + '?v='));
  return hit || ('/static/' + name);
}
"""

# Count occurrences of a marker across the ACTIVE terminal's whole xterm
# buffer (scrollback + viewport). Installed once as window.__bufCount.
_BUF_COUNT_SETUP = """
async () => {
  const live = """ + _LIVE_MODULE + """;
  const { state } = await import(live('state.js'));
  window.__bufCount = (marker) => {
    const t = state.terminal;
    if (!t || !t.term) return -1;
    const buf = t.term.buffer.active;
    let count = 0;
    for (let i = 0; i < buf.length; i++) {
      const line = buf.getLine(i);
      if (!line) continue;
      const text = line.translateToString(true);
      let idx = text.indexOf(marker);
      while (idx !== -1) {
        count += 1;
        idx = text.indexOf(marker, idx + marker.length);
      }
    }
    return count;
  };
}
"""


def _stable_marker_count(page: Page, marker: str) -> int:
    """Sample the buffer count until two consecutive reads agree."""
    last = page.evaluate("(m) => window.__bufCount(m)", marker)
    for _ in range(20):
        page.wait_for_timeout(500)
        cur = page.evaluate("(m) => window.__bufCount(m)", marker)
        if cur == last:
            return cur
        last = cur
    return last


def test_reconnect_replay_does_not_duplicate_scrollback(
    authed_page: Page,
    base_url: str,
    launched_claude_pty_session: str,
) -> None:
    """Regression pin for issue #444 (duplicated conversation tail).

    A reconnect replays the raw scrollback ring into the SAME reused xterm;
    without the server's clear-frame preamble the ring lands *below* the
    stale buffer, so every reconnect appended another copy of the whole
    conversation. Pin: type a distinctive marker (no newline — it just echoes
    in the agent's composer), force a WS drop, let the auto-reconnect replay
    land, and assert the marker count in the buffer did NOT grow.

    Uses the REAL Claude fixture (issue #534): the marker's on-screen echo
    comes from the agent painting its composer, and the replayed ring must
    carry that real rendered content — the one e2e assertion a stub child
    can't stand in for.
    """
    sid = launched_claude_pty_session
    authed_page.add_init_script(_WS_PROBE)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_function(
        "() => window.__wsInstances && window.__wsInstances.length >= 1 "
        "&& window.__wsInstances[0].readyState === 1",
        timeout=10_000,
    )
    authed_page.evaluate(_BUF_COUNT_SETUP)

    # Echo a marker into the agent's composer (no \n — never submitted, so
    # the agent doesn't act on it; short enough not to wrap on the iPhone
    # projection's narrow cols).
    marker = "dup-pin-444"
    authed_page.evaluate(
        "(text) => window.__wsInstances.at(-1).send("
        "JSON.stringify({ type: 'input', data: text }))",
        marker,
    )
    # The echo lands once the agent has painted its composer; give the
    # slower projections headroom.
    authed_page.wait_for_function(
        "(m) => window.__bufCount(m) >= 1", arg=marker, timeout=30_000
    )
    before = _stable_marker_count(authed_page, marker)
    assert before >= 1

    # Force-drop the live socket → the #28 backoff reconnects and the
    # session-host replays the ring.
    authed_page.evaluate("window.__wsInstances.at(-1).close()")
    authed_page.wait_for_function(
        "() => window.__wsInstances.length >= 2 "
        "&& window.__wsInstances.at(-1).readyState === 1",
        timeout=15_000,
    )
    # Protocol pin (#444): the reconnect's ring replay must arrive with the
    # clear-frame preamble prepended in the SAME frame, wiping the stale
    # buffer before the ring lands. This is what fails on an unfixed host —
    # a fresh session's ring still starts with the boot-time clear, so the
    # count check alone can miss the bug (it only bites once the ring
    # saturates past 256 KB and truncation eats that leading clear).
    authed_page.wait_for_function(
        "() => window.__wsInstances.at(-1).__firstMsg !== null",
        timeout=10_000,
    )
    first = authed_page.evaluate("window.__wsInstances.at(-1).__firstMsg")
    assert first is not None and first.startswith("\x1b[H\x1b[2J\x1b[3J"), (
        f"reconnect replay frame does not start with the clear-frame "
        f"preamble (got {first!r:.60}) — a saturated ring would land below "
        "the stale buffer and duplicate the conversation tail (#444)"
    )

    # Wait for the replayed marker to be back on screen, then for the
    # buffer to go quiet before counting.
    authed_page.wait_for_function(
        "(m) => window.__bufCount(m) >= 1", arg=marker, timeout=15_000
    )
    after = _stable_marker_count(authed_page, marker)

    assert after == before, (
        f"reconnect replay duplicated the scrollback: marker {marker!r} "
        f"appeared {before}x before the WS drop but {after}x after the "
        "replay — the ring landed below the stale buffer instead of on a "
        "wiped one (#444)"
    )


# --------------------------------------------------------------- issue #610

# A WebSocket that opens (after a tick, same async shape as a real socket)
# but never sends a single frame — the exact condition the #610 first-paint
# watchdog exists to catch. Deliberately not the real session-host: this
# isolates the pure client-side timer logic (arm on open, never disarmed
# because nothing ever arrives) from server behavior, which is what the fix
# actually is — the root cause on the server side was never pinned down.
_SILENT_WS = """
(() => {
  class SilentWs extends EventTarget {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = 0;
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      window.__silentWs = this;
      setTimeout(() => {
        this.readyState = 1;
        if (this.onopen) this.onopen({});
      }, 0);
    }
    send() {}
    close() {
      if (this.readyState === 3) return;
      this.readyState = 3;
      if (this.onclose) this.onclose({ code: 1000, reason: '', wasClean: true });
    }
  }
  SilentWs.CONNECTING = 0; SilentWs.OPEN = 1; SilentWs.CLOSING = 2; SilentWs.CLOSED = 3;
  window.WebSocket = SilentWs;
})();
"""


def test_terminal_watchdog_fires_when_nothing_ever_paints(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
) -> None:
    """#610: a terminal that opens a WS but receives no frame at all within
    the watchdog window must surface an explicit, actionable status — never
    stay silently blank forever. Real wall-clock wait (no virtual clock):
    installing one globally from page load risks freezing unrelated page
    timers (polling loops, the #374 first-fit defer) that this test has no
    business touching — the ~8s cost is in the same order as this file's
    other real-timing waits."""
    sid = launched_pty_session
    authed_page.add_init_script(_SILENT_WS)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    # Confirm the fake socket actually reached OPEN (proves ws.onopen ran,
    # which is what arms the watchdog) before waiting out its window.
    authed_page.wait_for_function(
        "() => window.__silentWs && window.__silentWs.readyState === 1",
        timeout=10_000,
    )

    # Past PAINT_WATCHDOG_MS (8000ms in terminal-connection.js) with margin.
    authed_page.wait_for_function(
        "() => { const s = document.getElementById('terminalStatus'); "
        "return s && !s.hidden && s.textContent.indexOf('No response yet') !== -1; }",
        timeout=11_000,
    )

    # And it must be recoverable — tapping it starts a fresh connect attempt
    # (a new WebSocket instance), not a dead end.
    authed_page.locator("#terminalStatus").click()
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus').textContent"
        ".indexOf('Connecting') !== -1",
        timeout=5_000,
    )
