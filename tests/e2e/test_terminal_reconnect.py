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

from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SESSIONS_DIR = _REPO_ROOT / "webapp" / "sessions"

# Wrap WebSocket so we can see every instance the SPA constructs. Runs
# before any page script, so connectWs() in terminal.js uses the wrapped
# constructor without any code change.
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


def _read_session_log(sid: str) -> str:
    log_path = _SESSIONS_DIR / f"{sid}.log"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def test_terminal_reconnects_after_ws_drop(
    authed_page: Page, base_url: str, launched_pty_session: str
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
    deadline_ms = 5_000
    found = False
    for _ in range(deadline_ms // 200):
        if "rec0nn3ct-marker-32" in _read_session_log(sid):
            found = True
            break
        authed_page.wait_for_timeout(200)
    assert found, (
        f"input sent on reconnected ws did not appear in {_SESSIONS_DIR / (sid + '.log')} "
        "within 5s — reconnect handshake succeeded but the new ws isn't carrying input"
    )
