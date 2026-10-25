"""Shared helpers for the Screen view tests: client addresses, WebSocket reads and fake sockets."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from typing import Dict, List

import pytest
from PIL import Image
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.desktop import capture as desktop_capture
from src.desktop import cursor as desktop_cursor
from src.desktop.capture import Monitor


LOOPBACK = "127.0.0.1"


TAILNET = "100.101.102.103"


LAN = "192.168.1.20"


TOKEN = "the-bearer-token"


def encoded(image: Image.Image, width: int, quality: int = desktop_capture.JPEG_QUALITY) -> bytes:
    """What ``stream_frames`` sends for ``image``: the same two stages it runs."""
    return desktop_capture.jpeg_bytes(desktop_capture.resize_to_width(image, width), quality)


PRIMARY = Monitor(id=0, left=0, top=0, width=64, height=36, primary=True)


# The tower's layout in miniature: a second monitor above the primary, so its
# origin is negative on the virtual desktop.
ABOVE = Monitor(id=1, left=0, top=-36, width=64, height=36, primary=False)


def _as(app, host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


def _ws_url(token: str = "", tt: str = "", monitor: str = "") -> str:
    params = [f"{k}={v}" for k, v in (("token", token), ("tt", tt), ("monitor", monitor)) if v]
    return "/api/screen/ws" + ("?" + "&".join(params) if params else "")


def _next_frame(ws) -> bytes:
    """The next binary frame, past any text (cursor, status) sent first."""
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
        if message.get("bytes") is not None:
            return message["bytes"]


def _next_text(ws, kind: str) -> Dict:
    """The next JSON text message of type ``kind``, past frames."""
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
        if message.get("text") is not None:
            body = json.loads(message["text"])
            if body.get("type") == kind:
                return body


def _ws_close_code(client: TestClient, url: str, headers=None) -> int:
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(url, headers=headers or {}) as ws:
            _next_frame(ws)
    return excinfo.value.code


def _wait_for(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def _close_and_wait(ws, audit) -> Dict:
    """Close from the client, as a phone does, and wait for the handler's
    own end; the close audit's fields.

    Leaving the ``with`` block instead makes TestClient cancel the handler,
    and anyio then re-cancels every await in its ``finally``, so the close
    audit's off-loop write can be lost (1 in 15 runs on the pre-#1521 code).
    uvicorn never cancels a handler on a client close; this matches it."""
    ws.close()
    _wait_for(
        lambda: "screen_close" in [c.args[0] for c in audit.audit_event.call_args_list],
        "the close audit",
    )
    return audit.audit_event.call_args_list[-1].kwargs


class _SilentSocket:
    """A socket that accepts everything and never sends a client message."""

    query_params: Dict[str, str] = {}

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def send_text(self, text: str) -> None:
        pass

    async def receive_text(self) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ScriptedSocket:
    """Plays back client messages, then disconnects."""

    def __init__(self, messages: List[str]) -> None:
        self._messages = list(messages)

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect(code=1001)


# ------------------------------------------------------- input (#1521)
#
# Every test here runs against tests/conftest.py's recording fake injector:
# nothing reaches the real desktop.

_CLICK = json.dumps({"type": "click", "button": "left", "x": 0.5, "y": 0.5})


_DOWN = json.dumps({"type": "down", "button": "left", "x": 0.25, "y": 0.25})


def _ws_input_refused(client: TestClient, url: str, headers=None) -> int:
    """Send input on a socket the gate refuses; the close code it got."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(url, headers=headers or {}) as ws:
            ws.send_text(_CLICK)
            ws.send_text(_DOWN)
            _next_frame(ws)
    return excinfo.value.code


# ------------------------------------------------------ clipboard (#1719)
#
# Every test here runs against tests/conftest.py's recording fake clipboard:
# nothing reaches the real one.


def _clipboard_frame(text: str) -> str:
    return json.dumps({"type": "clipboard_set", "text": text}, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------ capture loop


def _run(coro):
    return asyncio.run(coro)


async def _drive(grab, *, ack_every_frame: bool, run_for: float, interval=0.001, retry=0.001):
    """Run stream_frames for ``run_for`` seconds; return (frames, statuses)."""
    acked, stopped = asyncio.Event(), asyncio.Event()
    acked.set()
    frames: List[bytes] = []
    statuses: List[str] = []

    async def send_frame(data: bytes) -> None:
        frames.append(data)
        if ack_every_frame:
            acked.set()

    async def send_status(state: str) -> None:
        statuses.append(state)

    task = asyncio.create_task(
        desktop_capture.stream_frames(
            grab, send_frame, send_status, acked, stopped,
            width=64, interval=interval, retry=retry,
        )
    )
    await asyncio.sleep(run_for)
    stopped.set()
    returned = await asyncio.wait_for(task, timeout=2)
    assert returned == len(frames)
    return frames, statuses


def _fake_cursor_win32(monkeypatch, readings):
    """A stand-in user32 for :func:`desktop_cursor.cursor_reader`: each read
    pops ``(ok, flags, handle, x, y)``."""
    import ctypes
    from ctypes import wintypes

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", ctypes.c_void_p), ("ptScreenPos", wintypes.POINT)]

    def get_cursor_info(ref):
        ok, flags, handle, x, y = readings.pop(0)
        info = ref._obj
        assert info.cbSize == ctypes.sizeof(CURSORINFO)
        info.flags, info.hCursor = flags, handle
        info.ptScreenPos.x, info.ptScreenPos.y = x, y
        return ok

    user32 = types.SimpleNamespace(
        GetCursorInfo=get_cursor_info, LoadCursorW=lambda _inst, idc: idc.value + 1000,
    )
    monkeypatch.setattr(desktop_cursor, "_user32", lambda: (user32, CURSORINFO))
    monkeypatch.setattr(desktop_cursor.sys, "platform", "win32")
    monkeypatch.setattr(desktop_capture, "ensure_dpi_awareness", lambda: None)


async def _drive_cursor(read, *, run_for: float, interval=0.001, retry=0.001):
    stopped = asyncio.Event()
    sent: List[Dict] = []

    async def send(message: Dict) -> None:
        sent.append(message)

    task = asyncio.create_task(
        desktop_cursor.stream_cursor(read, send, stopped, interval=interval, retry=retry)
    )
    await asyncio.sleep(run_for)
    stopped.set()
    returned = await asyncio.wait_for(task, timeout=2)
    assert returned == len(sent)
    return sent


class _Win32Error(Exception):
    pass


def _fake_win32(monkeypatch, *, monitors=(), blit_error=False):
    """Stand-in pywin32 modules recording what the capture code asks of them."""
    calls = {"blit": [], "released": 0, "deleted": 0}

    pywintypes = types.SimpleNamespace(error=_Win32Error)

    def enum():
        return [(i, None, rect) for i, (rect, _flags) in enumerate(monitors)]

    win32api = types.SimpleNamespace(
        EnumDisplayMonitors=enum,
        GetMonitorInfo=lambda h: {"Monitor": monitors[h][0], "Flags": monitors[h][1]},
    )

    class Bitmap:
        size = (0, 0)

        def CreateCompatibleBitmap(self, _dc, w, h):
            Bitmap.size = (w, h)

        def GetBitmapBits(self, _as_bytes):
            w, h = Bitmap.size
            return bytes([0, 0, 255, 0]) * (w * h)

        def GetHandle(self):
            return 1

    class MemDC:
        def SelectObject(self, _obj):
            pass

        def BitBlt(self, dest, size, _src, src_origin, _rop):
            if blit_error:
                raise _Win32Error("BitBlt failed")
            calls["blit"].append((dest, size, src_origin))

        def DeleteDC(self):
            pass

    class ScreenDC(MemDC):
        def CreateCompatibleDC(self):
            return MemDC()

    win32ui = types.SimpleNamespace(
        error=_Win32Error,
        CreateDCFromHandle=lambda _h: ScreenDC(),
        CreateBitmap=Bitmap,
    )

    def release(_hwnd, _dc):
        calls["released"] += 1

    def delete(_h):
        calls["deleted"] += 1

    win32gui = types.SimpleNamespace(
        GetWindowDC=lambda _hwnd: 42, ReleaseDC=release, DeleteObject=delete
    )
    for name, mod in (
        ("pywintypes", pywintypes), ("win32api", win32api), ("win32ui", win32ui),
        ("win32gui", win32gui), ("win32con", types.SimpleNamespace(SRCCOPY=0xCC0020)),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(desktop_capture.sys, "platform", "win32")
    monkeypatch.setattr(desktop_capture, "ensure_dpi_awareness", lambda: None)
    return calls
