"""The Screen stream: close-line audit, capture and cursor loops that stop with the viewer."""

from __future__ import annotations

import asyncio
import time


from src import desktop_capture, desktop_cursor
from src.desktop_cursor import CursorState

from tests.api.screen._support import (
    ABOVE,
    LOOPBACK,
    TAILNET,
    TOKEN,
    _ScriptedSocket,
    _as,
    _close_and_wait,
    _next_frame,
    _next_text,
    _run,
    _ws_url,
)


def test_the_close_line_counts_bytes_and_unavailable(direct_screen_ws):
    run, audited = direct_screen_ws

    async def script(send_frame, send_status):
        await send_frame(b"x" * 10)
        await send_status("unavailable")
        await send_status("live")
        await send_frame(b"y" * 5)
        await send_status("unavailable")

    async def on_close():
        pass

    _run(run(script, on_close))
    (event, closed) = audited[-1]
    assert event == "screen_close"
    assert closed["frames"] == 2 and closed["bytes"] == 15
    assert closed["unavailable"] == 2


def test_the_close_line_survives_a_cancelled_handler(direct_screen_ws):
    """A handler cancelled while it writes the close line (server shutdown, or
    the test client's cancel scope) must still finish writing it (#1688)."""
    run, audited = direct_screen_ws

    async def go():
        entered = asyncio.Event()

        async def script(send_frame, _send_status):
            await send_frame(b"x" * 7)

        async def on_close():
            entered.set()
            await asyncio.sleep(0.05)

        handler = asyncio.create_task(run(script, on_close))
        await entered.wait()
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        await asyncio.sleep(0.2)  # the shielded write finishes on its own

    _run(go())
    closed = [fields for event, fields in audited if event == "screen_close"]
    assert len(closed) == 1, "the close line was lost with the cancelled handler"
    assert closed[0]["bytes"] == 7


def test_capture_stops_when_the_viewer_disconnects(screen, webapp_client):
    """With no viewer connected, no capture runs."""
    app, _, grabs = screen
    audit = webapp_client[2]["audit"]
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        _next_frame(ws)
        ws.send_text("ack")
        # Measure from the handler's own end (its close audit), not from the
        # client returning, which can come first.
        _close_and_wait(ws, audit)
    settled = len(grabs)
    assert settled >= 1
    time.sleep(desktop_capture.FRAME_INTERVAL_S * 4)
    assert len(grabs) == settled, "a grab ran after the viewer left"


def test_ws_sends_the_cursor_for_the_monitor_being_viewed(screen, fake_cursor):
    """The cursor rides the picture's socket, behind its gate, for the
    monitor on screen: a shape name and a spot, nothing else."""
    app, tt, _ = screen
    with _as(app, TAILNET).websocket_connect(_ws_url(TOKEN, tt, monitor="1")) as ws:
        cursor = _next_text(ws, "cursor")
    assert fake_cursor["readers"] == [ABOVE]
    assert cursor == {"type": "cursor", "x": 0.5, "y": 0.25, "shape": "text", "visible": True}


def test_ws_sends_a_cursor_move_with_no_frame(screen, fake_cursor):
    """A still picture sends no frame, but the cursor moving on it is sent."""
    app, _, grabs = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        # The first frame and the first cursor message, in either order.
        seen = set()
        while seen != {"frame", "cursor"}:
            message = ws.receive()
            seen.add("frame" if message.get("bytes") is not None else "cursor")
        frames_before = len(grabs)
        fake_cursor["state"] = CursorState(visible=True, x=0.75, y=0.5, shape="hand")
        moved = _next_text(ws, "cursor")
    assert moved["x"] == 0.75 and moved["shape"] == "hand"
    assert len(grabs) == frames_before, "no frame was acked, so none was grabbed"


def test_the_cursor_loop_stops_when_the_viewer_disconnects(screen, fake_cursor, webapp_client):
    app, _, _ = screen
    audit = webapp_client[2]["audit"]
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        _next_text(ws, "cursor")
        _close_and_wait(ws, audit)
    settled = fake_cursor["reads"]
    assert settled >= 1
    time.sleep(desktop_cursor.CURSOR_INTERVAL_S * 6)
    assert fake_cursor["reads"] == settled, "the cursor was read after the viewer left"


def test_a_viewer_disconnect_stops_the_capture_loop():
    """The route-level test above can't prove this alone: TestClient cancels
    the handler when the socket closes, uvicorn does not. Under uvicorn an
    idle loop sends nothing, so only the ack reader noticing the disconnect
    stops the grabs."""
    from app.webapp.routers import desktop_screen

    async def go():
        acked, stopped = asyncio.Event(), asyncio.Event()
        await desktop_screen._read_client(_ScriptedSocket(["ack"]), acked, stopped)
        return acked.is_set(), stopped.is_set()

    assert _run(go()) == (True, True)
