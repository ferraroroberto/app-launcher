"""Regression pin for commit 142e2b4 / issue #28 (live terminal WS reconnect).

The bug: iOS aggressively suspends backgrounded PWAs; uvicorn's ping
timeout then closes the half-dead WebSocket and the overlay was left
frozen on "Disconnected." until the user manually re-opened the
session. The fix factors the ws setup into ``connectWs(t)`` (now
``connectTerminalWs`` in ``terminal-connection.js``) and re-runs it after
non-final close codes with 1s/2s/4s/8s backoff.

This test exercises the JS half of the fix by:
  1. Wrapping ``window.WebSocket`` in an init script so the test can
     observe every WS the SPA opens (no product change).
  2. Opening the terminal via ``?terminal=<sid>`` deep-link.
  3. Force-closing the open WS from the page (code 1005). Any code the
     ``ws.onclose`` handler in ``connectTerminalWs`` doesn't special-case
     (4000/4401/4403/4404) falls through to ``scheduleReconnect`` — the
     "iOS-suspend" path, same one reached when uvicorn's ping timeout fires.
  4. Asserting the SPA opens a fresh WS and that input sent on the
     fresh socket reaches ``webapp/sessions/<sid>.log``.

Before the drop, the same page and socket carry the warm re-open pin (#430),
formerly ``test_warm_terminal_reopen.py``: see
``_leg_warm_reopen_reuses_terminal_and_ws``.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

# serial (#1231): the gate runs these after its parallel workers finish, not
# among them. They are the suite's most load-sensitive pins (#678: the #444
# replay pin drives a real Claude process; #58: reconnect input delivery), and
# at 4 workers the real agent's cold boot overran its budget.
pytestmark = [pytest.mark.smoke, pytest.mark.serial]

# How long to wait for a typed marker to echo back through a REAL Claude Code
# process's composer (issue #678). Unlike the UI-transition budgets this is
# not comparable to (#186's 15s default action timeout), this one has to
# cover a real agent's cold boot — on a box already running several other
# live agent PTYs plus the dual-projection browser suite, that boot can run
# long, especially on the slower WebKit/iPhone projection. Env-tunable like
# ``E2E_LOG_POLL_DEADLINE_MS`` (#184) and ``E2E_STOP_OVERLAY_HIDE_MS`` (#286)
# so a loaded host gets headroom without slowing the common-case local pass.
# 60s -> 90s (#887 follow-up), matching what CI already sets.
#
# Correcting a claim #887 committed here: it asserted the echo "was never the
# wait that blocked" and that the replay-frame wait below was the whole story.
# That was inferred from a red gate whose traceback had been truncated away,
# never actually read. The next full gate's traceback names *this* wait, at
# this line, timing out at 60s — so both waits were under-budgeted, and
# fixing only the replay frame left this one marginal.
#
# What the measurements actually show: typical is ~9s (sampled mid-run on the
# reference dev box), but a REAL Claude Code cold boot at the tail of a full
# ~19 min dual-projection gate exceeded 60s. The distribution has a long
# right tail, not a slow median, so the budget has to cover the tail. 90s is
# what CI already allows for the same reason.
#
# Honest limit: this widens the margin, it does not make the leg
# deterministic — a real agent's cold boot on a loaded box has no bounded
# worst case. A red here still reads as "check host load" (#678), and the
# right response is still to look at what else the machine is running rather
# than to widen again reflexively.
_REAL_AGENT_ECHO_MS = int(os.environ.get("E2E_REAL_AGENT_ECHO_MS", "90000"))

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


# The SPA loads its modules cache-busted (`state.js?v=<asset_hash>`). A bare
# import('/static/state.js') would evaluate a SECOND module instance with its
# own empty state (and, for terminal.js, its own empty warm cache) — silently
# testing a parallel universe. Resolve the page's real module URL from the
# resource timeline so the import shares the live instance.
_LIVE_MODULE = """
(name) => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/' + name + '?v='));
  return hit || ('/static/' + name);
}
"""


def _hide_overlay_and_assert_stashed(authed_page: Page) -> None:
    """Hide the terminal overlay and check the xterm element and WS were kept, not disposed."""
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


def _leg_warm_reopen_reuses_terminal_and_ws(
    authed_page: Page, sid: str, wait_for_session_log
) -> None:
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
    socket — no re-subscribe, no repaint nudge, no re-emission. This pins:

      1. The deep-link open created exactly one WS; the xterm element is tagged.
      2. hideTerminal() hides the overlay but leaves the tagged element in
         the DOM and the WS open.
      3. openTerminal() for the same sid re-shows the SAME element (tag
         intact), opens NO new WS, and input still reaches the PTY log over
         the original socket.
    """
    assert authed_page.evaluate("window.__wsInstances.length") == 1
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()

    # Tag the live xterm root so element identity survives the round-trip.
    authed_page.wait_for_selector("#terminalHost .xterm", timeout=OVERLAY_OPEN_MS)
    authed_page.evaluate(
        "document.querySelector('#terminalHost .xterm').dataset.warm = 'tagged'"
    )

    _hide_overlay_and_assert_stashed(authed_page)

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
        timeout=OVERLAY_OPEN_MS,
    )
    _leg_warm_reopen_reuses_terminal_and_ws(authed_page, sid, wait_for_session_log)

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
        timeout=OVERLAY_OPEN_MS,
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
    # The echo lands once a REAL Claude Code process has painted its
    # composer — a real cold boot, not a UI transition, so this budget is
    # env-tunable and wide (see _REAL_AGENT_ECHO_MS above), unlike the
    # slower projections' usual fixed headroom for pure UI waits.
    try:
        authed_page.wait_for_function(
            "(m) => window.__bufCount(m) >= 1",
            arg=marker,
            timeout=_REAL_AGENT_ECHO_MS,
        )
    except PlaywrightTimeoutError as exc:
        raise AssertionError(
            f"marker echo did not land within {_REAL_AGENT_ECHO_MS}ms — the "
            "real Claude Code process backing this session likely hasn't "
            "finished its cold boot (see #678); check host load before "
            "assuming this is a diff regression"
        ) from exc
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
        # A real session-host ring replay over a freshly reopened socket —
        # the same server-round-trip-to-paint class as opening the terminal,
        # so it shares that budget (#887). On the old 10s this was the wait
        # that actually failed at the tail of a loaded gate.
        timeout=OVERLAY_OPEN_MS,
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
        timeout=OVERLAY_OPEN_MS,
    )

    # Past PAINT_WATCHDOG_MS (8000ms in terminal-connection.js) with margin.
    # 11s gave that 8s window only 3s of slack, which is not margin at all on
    # a box whose page timers slip under load — this was the other half of
    # #887's residual flake. The watchdog is a page-side setTimeout, so the
    # budget has to cover the window *plus* however far the event loop has
    # drifted; OVERLAY_OPEN_MS on top of the window is the same headroom the
    # rest of the pool now gets.
    authed_page.wait_for_function(
        "() => { const s = document.getElementById('terminalStatus'); "
        "return s && !s.hidden && s.textContent.indexOf('No response yet') !== -1; }",
        timeout=8_000 + OVERLAY_OPEN_MS,
    )

    # And it must be recoverable — tapping it starts a fresh connect attempt
    # (a new WebSocket instance), not a dead end. Asserted on the socket
    # itself: the "Connecting…" it shows in between lasts only until the
    # stub's next open, and since #1219 a cleared status also empties its
    # text, so the old read of that word was catching leftover text.
    authed_page.evaluate("() => { window.__wsBeforeTap = window.__silentWs; }")
    authed_page.locator("#terminalStatus").click()
    authed_page.wait_for_function(
        "() => window.__silentWs && window.__silentWs !== window.__wsBeforeTap"
        " && document.getElementById('terminalStatus').textContent"
        ".indexOf('No response yet') === -1",
        timeout=5_000,
    )
