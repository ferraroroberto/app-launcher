"""Regression pin: stale repaint-batch state across a fullscreen reconnect.

Reported after #435 shipped: during an *active* Codex conversation (not a
cold resume — that path was already fixed and confirmed working), the phone
sometimes showed the true beginning of the conversation, then a chunk of
missing content, then the latest lines — a hole in the middle rather than a
simple truncation.

Root cause: WebSocket drops are routine on mobile (screen dim, a brief
background, a network blip) and every drop that isn't a clean session-end
schedules a silent reconnect (``terminal-connection.js`` ``scheduleReconnect``
/ ``ws.onclose``). Each successful reconnect's ``ws.onopen`` calls
``beginRepaintBatch(terminal)`` again to conceal the incoming snapshot. But
``beginRepaintBatch`` only reset ``batchTimer`` — it left a leftover
``batchQuietTimer`` (armed by the *previous, now-dead* connection's
``ws.onmessage``) still ticking, and never cleared a partially-filled
``batchBuf``. The dead connection's own snapshot (sent as two separate WS
messages: ``_CLEAR_FRAME`` then the history+frame payload) could have been
interrupted mid-delivery when the socket dropped, so ``batchBuf`` might hold
only the clear-frame with none of the actual content. If that stale timer
then fires, it flushes whatever happens to be sitting in the shared
``batchBuf`` at that moment — a snapshot from a dead connection, mixed with
or clobbered by the new connection's own writes, landing anywhere from a
partial paint to the exact "gap in the middle" symptom reported.

This test drives the real, exported ``beginRepaintBatch`` against a
minimal fake terminal object (no live PTY / WebSocket needed — the bug is
entirely a client-side state-machine issue) and proves a second call, made
to simulate the next connection's ``onopen``, must never see stale content
or a still-armed timer left over from the first.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

_LIVE_MODULE = """
(name) => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/' + name + '?v='));
  return hit || ('/static/' + name);
}
"""

_PROBE = """
async () => {
  const live = """ + _LIVE_MODULE + """;
  const mod = await import(live('terminal-connection.js'));

  const terminal = {
    isFullscreen: true,
    term: { element: document.createElement('div') },
  };

  // Connection A: a batch starts (its ws.onopen), one message arrives and
  // arms the quiet timer (ws.onmessage's real batching branch) — then the
  // connection dies before that timer fires and before it ever flushes,
  // exactly as a dropped WebSocket would leave things.
  mod.beginRepaintBatch(terminal);
  terminal.batchBuf.push('STALE-FROM-DEAD-CONNECTION-A');
  terminal.batchQuietTimer = setTimeout(function () {}, 60000);
  const staleTimerHandle = terminal.batchQuietTimer;

  // Connection B: the reconnect succeeds and its own ws.onopen fires
  // beginRepaintBatch again — this must start a clean slate.
  mod.beginRepaintBatch(terminal);

  return {
    batchBufAfterSecondOpen: terminal.batchBuf.slice(),
    quietTimerWasReplaced: terminal.batchQuietTimer !== staleTimerHandle,
    quietTimerNowNull: terminal.batchQuietTimer === null,
  };
}
"""


def test_reconnect_repaint_batch_discards_stale_state(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    r = authed_page.evaluate(_PROBE)

    assert r["batchBufAfterSecondOpen"] == [], (
        f"a fresh reconnect's beginRepaintBatch() still carried "
        f"{r['batchBufAfterSecondOpen']!r} from a dead prior connection — "
        "a stale quiet-timer flushing this later corrupts the paint with "
        "content from two different connections (the reported "
        "'beginning visible, middle missing' symptom)"
    )
    assert r["quietTimerNowNull"], (
        "the dead connection's quiet timer must be cancelled by the next "
        "beginRepaintBatch(), not left armed to fire later against "
        "whatever batchBuf happens to hold by then"
    )
