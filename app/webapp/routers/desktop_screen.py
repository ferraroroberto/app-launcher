"""Screen view (#1520): the PC's desktop, live, on the phone.

``GET /api/screen/monitors`` lists the displays; ``WS /api/screen/ws`` streams
one of them as JPEG binary frames. Capture lives in :mod:`src.desktop.capture`
and runs only inside a connected socket.

A picture of the desktop shows whatever is on it, so this is terminal-grade
content (#1036): the HTTP route carries a ``passkey`` row in
``middleware._TERMINAL_GUARD_RULES``, and the WebSocket, which the HTTP
middleware never sees, re-applies the same gate before a single grab via
``middleware.ws_terminal_gate`` — the same helper ``sessions.proxy_session_ws``
calls, so the legs, order and close codes read the same way on both.

Protocol: the server sends binary JPEG frames and JSON text status messages
(``{"type": "status", "state": "unavailable" | "live"}``); the client sends
the text ``ack`` after drawing each frame, and the next frame waits for it.

Input (#1521): every other text frame the client sends is a JSON input
message (click, down/move/up, scroll, text, key), handed to a
:class:`src.desktop.input.InputSession` for this socket's monitor. The reader
that receives them only starts after the gate above has passed, so input
rides the same socket and the same gate as the picture; there is no other
input path (``tests/unit/desktop/test_desktop_input.py`` fails if one appears). When the
socket goes away, every key and button the session still holds is released.

Clipboard (#1719): a ``clipboard_set`` input message puts its text on the PC's
clipboard (its own size cap, its own seam, reached through the input
session like every other message);
the server answers with one JSON ``clipboard`` message that says whether it
worked, never what was copied. The close audit line counts transfers by
direction and records nothing of their content.

Quality (#1721): the connect query's ``preset`` (sharp, balanced or saver;
anything else is balanced) picks the JPEG quality, the pause between frames
and a width cap from one table in :mod:`src.desktop.capture`. The client can
switch a readout on with ``{"type": "readout", "on": true}``, a control
message that is not input; while it is on, the server sends one JSON ``stats``
message about once a second with the mean time of each capture stage
(grab, digest, resize, encode, in ms), and nothing at all while it is off.

Cursor (#1598): beside the frames, the server sends JSON ``cursor`` messages
(position as 0-1 in the shown monitor, a shape name, visible or not) from
:func:`src.desktop.cursor.stream_cursor`, whenever the cursor changed. It is
not paced by the frame ack, starts only after the gate, and stops with the
socket, like the frames.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from src import audit
from src.desktop import capture as desktop_capture
from src.desktop import cursor as desktop_cursor
from src.desktop import input as desktop_input

from app.webapp.middleware import ws_terminal_gate
from app.webapp.routers._helpers import audit_off_loop, client_ip_ws

logger = logging.getLogger(__name__)

router = APIRouter()

WS_PATH = "/api/screen/ws"


@router.get("/api/screen/monitors")
async def screen_monitors() -> Dict[str, Any]:
    """The displays the Screen view can show; no capture happens here."""
    monitors = await asyncio.to_thread(desktop_capture.list_monitors)
    return {"monitors": [m.to_dict() for m in monitors]}


def _str_param(websocket: WebSocket, name: str) -> str:
    return websocket.query_params.get(name, "").strip()


def _int_param(websocket: WebSocket, name: str) -> Optional[int]:
    raw = websocket.query_params.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def _read_client(
    websocket: WebSocket,
    acked: asyncio.Event,
    stopped: asyncio.Event,
    session: Optional[desktop_input.InputSession] = None,
    send: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    readout: Optional[asyncio.Event] = None,
) -> None:
    """Turn the client's ``ack`` messages into ``acked``, its readout switch
    into ``readout`` (set while the readout is on), and hand every other text
    frame to ``session`` as input, in order; what handling it queues for the
    phone (a clipboard transfer's outcome) goes out through ``send``. A
    disconnect stops.

    Input runs off the loop: ``SendInput`` is quick, but a slow low-level
    hook elsewhere on the PC can hold it, and that must not stall the app.
    """
    try:
        while True:
            text = await websocket.receive_text()
            if text == "ack":
                acked.set()
                continue
            switch = desktop_capture.parse_readout(text)
            if switch is not None:
                if readout is not None and switch:
                    readout.set()
                elif readout is not None:
                    readout.clear()
            elif session is not None:
                await asyncio.to_thread(session.handle_text, text)
                for reply in session.take_replies():
                    if send is not None:
                        await send(reply)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stopped.set()


async def _stream_cursor(
    read: Callable[[], desktop_cursor.CursorState],
    send: Callable[[Dict[str, Any]], Awaitable[None]],
    stopped: asyncio.Event,
) -> None:
    """The cursor loop; a send on a socket that just went away ends it, and
    the reader stops everything else."""
    try:
        await desktop_cursor.stream_cursor(read, send, stopped)
    except (WebSocketDisconnect, RuntimeError, OSError) as exc:
        logger.debug("Screen cursor loop ended while sending: %s", exc)
    except Exception as exc:  # noqa: BLE001 - the picture keeps streaming
        logger.warning("⚠️ Screen: cursor loop failed, cursor stays hidden: %s", exc)


async def _stream_stats(
    stats: desktop_capture.StageStats,
    readout: asyncio.Event,
    send: Callable[[Dict[str, Any]], Awaitable[None]],
    stopped: asyncio.Event,
) -> None:
    """Once a second, send the capture stages' mean times, only while the
    readout is on. The period is taken (and so reset) either way, so the first
    message after a switch-on covers the last second, not the whole session."""
    try:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=desktop_capture.STATS_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            message = stats.take()
            if message is not None and readout.is_set() and not stopped.is_set():
                await send(message)
    except (WebSocketDisconnect, RuntimeError, OSError) as exc:
        logger.debug("Screen stats loop ended while sending: %s", exc)


@router.websocket(WS_PATH)
async def screen_ws(websocket: WebSocket) -> None:
    """Stream one monitor until the viewer goes away."""
    # Accept first, then gate, so the browser gets the close code and reason.
    await websocket.accept()
    refusal = ws_terminal_gate(websocket, label="screen")
    if refusal is not None:
        await websocket.close(code=refusal[0], reason=refusal[1])
        return

    monitors = await asyncio.to_thread(desktop_capture.list_monitors)
    monitor = desktop_capture.pick_monitor(monitors, _int_param(websocket, "monitor"))
    if monitor is None:
        await websocket.close(code=4404, reason="no such monitor")
        return
    preset = desktop_capture.pick_preset(_str_param(websocket, "preset"))
    width = desktop_capture.frame_width(_int_param(websocket, "width"), monitor.width, preset)

    client = client_ip_ws(websocket)
    started = time.monotonic()
    await audit_off_loop(
        audit.audit_event, "screen_open", client=client, monitor=monitor.id,
        rect=list(monitor.rect), width=width, preset=preset.name,
    )

    acked = asyncio.Event()
    acked.set()
    stopped = asyncio.Event()
    readout = asyncio.Event()
    stats = desktop_capture.StageStats()

    # Counted here rather than taken from stream_frames' return value, so a
    # send that fails mid-stream still audits what did go out. The byte total
    # is the session's real traffic on the link, and ``unavailable`` counts the
    # times the desktop stopped being readable: the one durable trace of a
    # blocked capture, since the tray discards the webapp's own log output.
    sent = {"frames": 0, "bytes": 0, "unavailable": 0}
    # Frames and cursor messages come from two loops; one message at a time
    # goes out on the socket.
    send_lock = asyncio.Lock()

    async def send_frame(data: bytes) -> None:
        async with send_lock:
            await websocket.send_bytes(data)
        sent["frames"] += 1
        sent["bytes"] += len(data)

    async def send_status(state: str) -> None:
        if state == "unavailable":
            sent["unavailable"] += 1
        async with send_lock:
            await websocket.send_text(json.dumps({"type": "status", "state": state}))

    async def send_json(message: Dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(message))

    session = desktop_input.InputSession(desktop_input.get_injector(), monitor, monitors)
    reader = asyncio.create_task(
        _read_client(websocket, acked, stopped, session, send_json, readout)
    )
    stats_task = asyncio.create_task(_stream_stats(stats, readout, send_json, stopped))
    cursor = asyncio.create_task(
        _stream_cursor(desktop_cursor.cursor_reader(monitor), send_json, stopped)
    )
    try:
        await desktop_capture.stream_frames(
            lambda: desktop_capture.grab_monitor(monitor),
            send_frame, send_status, acked, stopped, width=width,
            interval=preset.interval, quality=preset.quality, stats=stats,
        )
    except (WebSocketDisconnect, RuntimeError, OSError) as exc:
        logger.debug("Screen socket ended while sending: %s", exc)
    finally:
        stopped.set()
        # First, and synchronously: nothing below may stop a dropped phone
        # from lifting a held Ctrl or mouse button on the PC.
        try:
            session.release_all()
        except Exception as exc:  # noqa: BLE001 - the close audit must still run
            logger.warning("⚠️ Screen: releasing held input failed: %s", exc)
        for task in (reader, cursor, stats_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Shielded: a handler cancelled mid-teardown (server shutdown, or a
        # test client's cancel scope, which re-cancels at every await) must
        # still write the close line, since it carries the session's traffic.
        await asyncio.shield(audit_off_loop(
            audit.audit_event, "screen_close", client=client, monitor=monitor.id,
            duration_s=round(time.monotonic() - started, 1), frames=sent["frames"],
            bytes=sent["bytes"], unavailable=sent["unavailable"],
            inputs=session.count, clipboard_to_pc=session.clipboard_to_pc,
        ))
