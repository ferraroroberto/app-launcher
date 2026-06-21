"""Issue #128: full-screen TUI (re)connect handling.

Two halves of the fix live in ``app/session_host/server.py``:

- the WS handler **skips the raw scrollback-ring replay** for a
  full-screen differential-TUI agent (Codex/ratatui) — replaying its
  stale deltas garbles a fresh xterm and re-answers the agent's startup
  terminal queries as input (the ``[?1;2c`` DA leak), while an inline
  agent (Claude Code) still gets its snapshot;
- ``_force_repaint`` nudges the TUI into a clean redraw by toggling the
  PTY width one column and back.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.session_host import server
from src.session_host import _EOF


async def _async_noop(*_args, **_kwargs) -> None:
    return None


class _StubSession:
    """Records the resize calls ``_force_repaint`` makes."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.resizes: list = []

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))
        self.rows, self.cols = rows, cols


# ----------------------------------------------------------- _force_repaint


@pytest.mark.asyncio
async def test_force_repaint_toggles_width_and_restores(monkeypatch):
    monkeypatch.setattr(server.asyncio, "sleep", _async_noop)
    sess = _StubSession(rows=50, cols=42)

    await server._force_repaint(sess)

    # One column down (a guaranteed SIGWINCH even on same-size reconnect),
    # then back to the real width.
    assert sess.resizes == [(50, 41), (50, 42)]
    assert (sess.rows, sess.cols) == (50, 42)


@pytest.mark.asyncio
async def test_force_repaint_clamps_one_column_width(monkeypatch):
    monkeypatch.setattr(server.asyncio, "sleep", _async_noop)
    sess = _StubSession(rows=10, cols=1)

    await server._force_repaint(sess)

    # cols-1 would be 0 — clamp to >=1 so the toggle never goes invalid.
    assert sess.resizes == [(10, 1), (10, 1)]


@pytest.mark.asyncio
async def test_force_repaint_swallows_errors(monkeypatch):
    monkeypatch.setattr(server.asyncio, "sleep", _async_noop)
    boom = MagicMock(rows=40, cols=120)
    boom.resize.side_effect = RuntimeError("dead pty")

    # Best-effort — a dead PTY must never propagate out of the nudge.
    await server._force_repaint(boom)


# ------------------------------------------------------------- replay gating


class _FakeSession:
    kind = "pty"
    rows = 40
    cols = 120

    def __init__(self, agent: str) -> None:
        self.agent = agent

    def subscribe(self):
        # _EOF pre-loaded so _pump_to_client closes (4000) right after the
        # snapshot decision, ending the handler deterministically.
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(_EOF)
        return "RAW-RING-SNAPSHOT", q

    def unsubscribe(self, _q) -> None:
        pass


def _connect(monkeypatch, agent: str):
    # The repaint nudge is exercised separately — stub it so the scheduled
    # task can't outlive the test.
    monkeypatch.setattr(server, "_force_repaint", _async_noop)
    monkeypatch.setattr(server.manager, "get", lambda sid: _FakeSession(agent))
    return TestClient(server.app)


def test_ws_skips_ring_replay_for_fullscreen_agent(monkeypatch):
    """Codex (fullscreen): the raw ring is NOT replayed. The only text frame
    is the clean-frame preamble (#270 tail-jump) — never the stale ring — so
    the DA-query leak can't happen (issue #128) and the reopened session lands
    on a clean buffer instead of crawling through history."""
    client = _connect(monkeypatch, "codex")
    with client.websocket_connect("/sessions/abc/ws?role=phone") as ws:
        first = ws.receive_text()
        assert first == server._CLEAR_FRAME
        assert first != "RAW-RING-SNAPSHOT"  # the stale ring is never sent
        assert "\x1b[3J" in first  # erase-scrollback, not just clear-screen
        with pytest.raises(WebSocketDisconnect):
            # Nothing else precedes the close: only the clean preamble.
            ws.receive_text()


def test_clean_frame_preamble_is_csi_only():
    """The preamble must be pure CSI — no OSC/DA — so it can never
    reintroduce the colour-query / DA leak the #128/#270 strip removed."""
    assert "\x1b]" not in server._CLEAR_FRAME  # no OSC introducer
    assert not server._CLEAR_FRAME.endswith("c")  # not a DA-shaped reply


def test_ws_replays_ring_for_inline_agent(monkeypatch):
    """Claude (inline): the scrollback snapshot is replayed exactly as
    before — its transcript is forgiving and worth keeping."""
    client = _connect(monkeypatch, "claude")
    with client.websocket_connect("/sessions/abc/ws?role=phone") as ws:
        assert ws.receive_text() == "RAW-RING-SNAPSHOT"
