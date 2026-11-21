"""Screen-first terminal replay: frame order, nothing lost or repeated.

A client that asks for ``order=screen`` gets the ring's tail (the current
screen) first, then a history marker, then the full replay byte for byte.
A client that does not ask, which is every older build, keeps the single
frame. The webapp proxy passes the order through to the session-host.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.webapp.session_host import server
from src.clients.session import ws_url
from src.session_host import _EOF
from src.session_hosting.replay_order import (
    HISTORY_MARKER,
    SCREEN_TAIL_MAX_CHARS,
    replay_frames,
    screen_tail_start,
)

CLEAR = server._CLEAR_FRAME


def _ring(lines: int) -> str:
    return "".join(f"\x1b[32mline {i:05d}\x1b[0m {'x' * 60}\r\n" for i in range(lines))


def test_big_ring_sends_screen_then_marker_then_full_replay():
    ring = _ring(5000)  # ~400 KB, past the 256 KB a real ring holds
    frames = replay_frames(ring, rows=40, clear=CLEAR)

    assert len(frames) == 3
    screen, marker, history = frames
    assert marker == HISTORY_MARKER
    # The full replay is the old single frame exactly: nothing lost or doubled.
    assert history == CLEAR + ring
    # The screen frame is the ring's own tail, cut at a line start.
    tail = screen[len(CLEAR):]
    assert screen.startswith(CLEAR) and ring.endswith(tail)
    assert ring[len(ring) - len(tail) - 1] == "\n"
    # Two lines per viewport row; the ring's final newline is one of them.
    assert tail.count("\n") == 2 * 40 - 1
    assert len(screen) <= len(CLEAR) + SCREEN_TAIL_MAX_CHARS


def test_screen_tail_stays_under_budget_with_long_lines():
    ring = ("y" * 9000 + "\r\n") * 60
    start = screen_tail_start(ring, rows=40)
    assert 0 < start and len(ring) - start <= SCREEN_TAIL_MAX_CHARS
    assert ring[start - 1] == "\n"


@pytest.mark.parametrize(
    "ring",
    [
        _ring(10),  # fits the screen budget: nothing to split
        "z" * (SCREEN_TAIL_MAX_CHARS * 3),  # no line start to cut at
        "",
    ],
    ids=["small", "no-newline", "empty"],
)
def test_ring_without_a_useful_split_keeps_the_single_frame(ring):
    assert replay_frames(ring, rows=40, clear=CLEAR) == [CLEAR + ring]


class _RingSession:
    kind = "pty"
    agent = "claude"
    rows = 40
    cols = 120

    def __init__(self, ring: str) -> None:
        self._ring = ring

    def subscribe(self):
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(_EOF)
        return self._ring, queue

    def unsubscribe(self, _queue) -> None:
        pass


def _frames(monkeypatch, ring: str, query: str) -> list:
    monkeypatch.setattr(server.manager, "get", lambda sid: _RingSession(ring))
    received = []
    with TestClient(server.app).websocket_connect(f"/sessions/abc/ws?{query}") as ws:
        with pytest.raises(WebSocketDisconnect):
            while True:
                received.append(ws.receive_text())
    return received


def test_ws_screen_first_order_on_request(monkeypatch):
    ring = _ring(5000)
    frames = _frames(monkeypatch, ring, "role=phone&order=screen")
    assert frames == replay_frames(ring, rows=40, clear=CLEAR)
    assert frames[1] == HISTORY_MARKER and frames[2] == CLEAR + ring


def test_ws_without_order_keeps_the_single_replay(monkeypatch):
    """An older client never sends ``order``: it gets the old frame."""
    ring = _ring(5000)
    assert _frames(monkeypatch, ring, "role=phone") == [CLEAR + ring]


def test_proxy_url_passes_the_order_through():
    assert ws_url(8446, "abc", "phone") == "ws://127.0.0.1:8446/sessions/abc/ws?role=phone"
    assert ws_url(8446, "abc", "pc", order="screen") == (
        "ws://127.0.0.1:8446/sessions/abc/ws?role=pc&order=screen"
    )
    assert ws_url(8446, "abc", order="a&b").endswith("&order=a%26b")
