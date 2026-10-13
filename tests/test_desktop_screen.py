"""Screen view (#1520): the gate on both protocols, and the capture loop.

The gate tests drive real client addresses through ``TestClient(client=...)``
(loopback, a tailnet CGNAT address, a LAN address) instead of patching the
host sets, so each one exercises the same code path a phone does. Every
refusal test presents everything *except* the leg it is about, so it fails if
and only if that leg is missing. Each refusal also asserts no grab happened:
a refused viewer must never cause a capture.

The loop tests drive :func:`src.desktop_capture.stream_frames` with fake
grabbers: a static screen sends one frame and then nothing, the next frame
waits for the client's ack, and an unreadable desktop is reported as its own
state.
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
import types
from pathlib import Path
from typing import List

import pytest
from PIL import Image
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src import desktop_capture
from src.desktop_capture import CaptureUnavailable, Monitor
from src.webauthn_gate import WebAuthnGate

LOOPBACK = "127.0.0.1"
TAILNET = "100.101.102.103"
LAN = "192.168.1.20"
TOKEN = "the-bearer-token"

PRIMARY = Monitor(id=0, left=0, top=0, width=64, height=36, primary=True)
# The tower's layout in miniature: a second monitor above the primary, so its
# origin is negative on the virtual desktop.
ABOVE = Monitor(id=1, left=0, top=-36, width=64, height=36, primary=False)


@pytest.fixture
def screen(webapp_client, monkeypatch, tmp_path: Path):
    """App with the bearer token and the passkey gate both live, two fake
    monitors, and a recording fake grabber. Yields ``(app, tt, grabs)``."""
    _client, app, _overrides = webapp_client
    cfg = app.state.webapp_config
    cfg.auth_token = TOKEN
    cfg.api_tokens = []
    cfg.webauthn_rp_id = "pc.example.ts.net"
    cfg.webauthn_origin = "https://pc.example.ts.net:8445"
    gate = WebAuthnGate(devices_path=tmp_path / "devices.json")
    app.state.webauthn_gate = gate
    tt = gate.mint_local_token()

    grabs: List[Monitor] = []

    def fake_grab(monitor: Monitor) -> Image.Image:
        grabs.append(monitor)
        return Image.new("RGB", (monitor.width, monitor.height), (10, 20, 30))

    monkeypatch.setattr(desktop_capture, "list_monitors", lambda: [PRIMARY, ABOVE])
    monkeypatch.setattr(desktop_capture, "grab_monitor", fake_grab)
    yield app, tt, grabs


def _as(app, host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


def _ws_url(token: str = "", tt: str = "", monitor: str = "") -> str:
    params = [f"{k}={v}" for k, v in (("token", token), ("tt", tt), ("monitor", monitor)) if v]
    return "/api/screen/ws" + ("?" + "&".join(params) if params else "")


def _ws_close_code(client: TestClient, url: str, headers=None) -> int:
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(url, headers=headers or {}) as ws:
            ws.receive_bytes()
    return excinfo.value.code


# ------------------------------------------------------------- HTTP gate


def test_the_guard_table_classifies_every_screen_path_as_passkey():
    from app.webapp.middleware import _terminal_guard_level

    assert _terminal_guard_level("/api/screen/monitors") == "passkey"
    assert _terminal_guard_level("/api/screen/ws") == "passkey"


def test_monitors_refused_over_cloudflare(screen):
    app, tt, _ = screen
    resp = _as(app, TAILNET).get(
        "/api/screen/monitors",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Terminal-Token": tt, "cf-ray": "abc-AMS"},
    )
    assert resp.status_code == 403


def test_monitors_refused_off_tailnet(screen):
    app, tt, _ = screen
    resp = _as(app, LAN).get(
        "/api/screen/monitors",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Terminal-Token": tt},
    )
    assert resp.status_code == 403


def test_monitors_refused_without_a_passkey_unlock(screen):
    app, _, _ = screen
    resp = _as(app, TAILNET).get(
        "/api/screen/monitors", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "passkey unlock required"


def test_monitors_for_an_unlocked_tailnet_client_and_capture_nothing(screen):
    app, tt, grabs = screen
    resp = _as(app, TAILNET).get(
        "/api/screen/monitors",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Terminal-Token": tt},
    )
    assert resp.status_code == 200
    monitors = resp.json()["monitors"]
    assert [m["id"] for m in monitors] == [0, 1]
    assert monitors[1]["top"] == -36 and monitors[1]["primary"] is False
    assert grabs == [], "listing monitors must not capture the screen"


def test_monitors_for_the_pc_itself_without_credentials(screen):
    app, _, _ = screen
    assert _as(app, LOOPBACK).get("/api/screen/monitors").status_code == 200


# --------------------------------------------------------------- WS gate


def test_ws_refused_over_cloudflare(screen):
    app, tt, grabs = screen
    code = _ws_close_code(
        _as(app, TAILNET), _ws_url(TOKEN, tt), headers={"cf-ray": "abc-AMS"}
    )
    assert code == 4403
    assert grabs == []


def test_ws_edge_header_overrides_a_loopback_address(screen):
    """cloudflared dials the webapp over loopback; its header must still win."""
    app, _, grabs = screen
    code = _ws_close_code(_as(app, LOOPBACK), _ws_url(), headers={"cf-ray": "abc-AMS"})
    assert code == 4403
    assert grabs == []


def test_ws_refused_off_tailnet(screen):
    app, tt, grabs = screen
    assert _ws_close_code(_as(app, LAN), _ws_url(TOKEN, tt)) == 4403
    assert grabs == []


def test_ws_refused_without_the_bearer_token(screen):
    app, tt, grabs = screen
    assert _ws_close_code(_as(app, TAILNET), _ws_url("", tt)) == 4401
    assert grabs == []


def test_ws_refused_without_a_passkey_unlock(screen):
    app, _, grabs = screen
    assert _ws_close_code(_as(app, TAILNET), _ws_url(TOKEN, "")) == 4401
    assert grabs == []


def test_ws_refused_with_a_forged_passkey_token(screen):
    app, _, grabs = screen
    assert _ws_close_code(_as(app, TAILNET), _ws_url(TOKEN, "not-minted")) == 4401
    assert grabs == []


def test_ws_streams_to_an_unlocked_tailnet_client(screen):
    app, tt, grabs = screen
    with _as(app, TAILNET).websocket_connect(_ws_url(TOKEN, tt)) as ws:
        frame = ws.receive_bytes()
    assert frame[:2] == b"\xff\xd8", "a JPEG frame"
    assert grabs and grabs[0] == PRIMARY, "no monitor asked for: the primary"


def test_ws_streams_to_the_pc_itself_without_credentials(screen):
    app, _, _ = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        assert ws.receive_bytes()[:2] == b"\xff\xd8"


def test_ws_selects_the_monitor_above_the_primary(screen):
    app, _, grabs = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url(monitor="1")) as ws:
        frame = ws.receive_bytes()
    assert grabs[0] == ABOVE and grabs[0].top < 0
    with Image.open(io.BytesIO(frame)) as img:
        assert img.size == (ABOVE.width, ABOVE.height)


def test_ws_unknown_monitor_is_its_own_close(screen):
    app, _, grabs = screen
    assert _ws_close_code(_as(app, LOOPBACK), _ws_url(monitor="7")) == 4404
    assert grabs == []


def test_ws_audits_connect_and_disconnect(screen, webapp_client):
    app, _, _ = screen
    audit = webapp_client[2]["audit"]
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        ws.receive_bytes()
    # The close line is written off the loop in the handler's finally, which
    # can still be running when the client side of the socket has returned.
    deadline = time.monotonic() + 5
    while len(audit.audit_event.call_args_list) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    events = [c.args[0] for c in audit.audit_event.call_args_list]
    assert events == ["screen_open", "screen_close"]
    closed = audit.audit_event.call_args_list[1].kwargs
    assert closed["client"] == LOOPBACK and closed["monitor"] == 0
    assert closed["frames"] == 1 and closed["duration_s"] >= 0


def test_capture_stops_when_the_viewer_disconnects(screen, webapp_client):
    """With no viewer connected, no capture runs."""
    app, _, grabs = screen
    audit = webapp_client[2]["audit"]
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        ws.receive_bytes()
        ws.send_text("ack")
    # Measure from the handler's own end (its close audit), not from the
    # client returning, which can come first.
    deadline = time.monotonic() + 5
    while (
        "screen_close" not in [c.args[0] for c in audit.audit_event.call_args_list]
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    settled = len(grabs)
    assert settled >= 1
    time.sleep(desktop_capture.FRAME_INTERVAL_S * 4)
    assert len(grabs) == settled, "a grab ran after the viewer left"


class _ScriptedSocket:
    """Plays back client messages, then disconnects."""

    def __init__(self, messages: List[str]) -> None:
        self._messages = list(messages)

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect(code=1001)


def test_a_viewer_disconnect_stops_the_capture_loop():
    """The route-level test above can't prove this alone: TestClient cancels
    the handler when the socket closes, uvicorn does not. Under uvicorn an
    idle loop sends nothing, so only the ack reader noticing the disconnect
    stops the grabs."""
    from app.webapp.routers import desktop_screen

    async def go():
        acked, stopped = asyncio.Event(), asyncio.Event()
        await desktop_screen._read_acks(_ScriptedSocket(["ack"]), acked, stopped)
        return acked.is_set(), stopped.is_set()

    assert _run(go()) == (True, True)


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


def test_an_idle_screen_sends_one_frame_then_nothing():
    grabs = []

    def static_grab():
        grabs.append(1)
        return Image.new("RGB", (64, 36), (1, 2, 3))

    frames, statuses = _run(_drive(static_grab, ack_every_frame=True, run_for=0.3))
    assert len(grabs) > 5, "the loop kept looking"
    assert len(frames) == 1, "an unchanged screen must not be resent"
    assert statuses == []


def test_a_change_is_sent():
    shade = iter(range(1000))

    def changing_grab():
        return Image.new("RGB", (64, 36), (next(shade) % 256, 0, 0))

    frames, _ = _run(_drive(changing_grab, ack_every_frame=True, run_for=0.2))
    assert len(frames) > 3


def test_the_next_frame_waits_for_the_ack():
    grabs = []
    shade = iter(range(1000))

    def changing_grab():
        grabs.append(1)
        return Image.new("RGB", (64, 36), (next(shade) % 256, 0, 0))

    frames, _ = _run(_drive(changing_grab, ack_every_frame=False, run_for=0.2))
    assert len(frames) == 1
    assert len(grabs) == 1, "no grab may run while a frame is unacknowledged"


def test_an_unreadable_desktop_is_reported_once_then_recovers():
    calls = {"n": 0}

    def flaky_grab():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise CaptureUnavailable("locked")
        return Image.new("RGB", (64, 36), (9, 9, 9))

    frames, statuses = _run(_drive(flaky_grab, ack_every_frame=True, run_for=0.2))
    assert statuses == ["unavailable", "live"]
    assert len(frames) == 1


def test_frames_are_downscaled_to_the_asked_width_never_up():
    big = Image.new("RGB", (400, 200), (5, 5, 5))
    with Image.open(io.BytesIO(desktop_capture.encode_jpeg(big, 100))) as img:
        assert img.size == (100, 50)
    with Image.open(io.BytesIO(desktop_capture.encode_jpeg(big, 800))) as img:
        assert img.size == (400, 200)


def test_width_is_clamped():
    assert desktop_capture.clamp_width(None) == desktop_capture.DEFAULT_WIDTH
    assert desktop_capture.clamp_width(10) == desktop_capture.MIN_WIDTH
    assert desktop_capture.clamp_width(99999) == desktop_capture.MAX_WIDTH


def test_pick_monitor_defaults_to_the_primary():
    assert desktop_capture.pick_monitor([ABOVE, PRIMARY], None) == PRIMARY
    assert desktop_capture.pick_monitor([ABOVE, PRIMARY], 1) == ABOVE
    assert desktop_capture.pick_monitor([ABOVE, PRIMARY], 5) is None
    assert desktop_capture.pick_monitor([], None) is None


# ------------------------------------------------- the Win32 edge, faked


def test_bgrx_buffer_becomes_rgb():
    # One red pixel then one blue pixel, as GetBitmapBits lays them out.
    bits = bytes([0, 0, 255, 0, 255, 0, 0, 0])
    img = desktop_capture.bgrx_to_image(bits, 2, 1)
    assert img.getpixel((0, 0)) == (255, 0, 0)
    assert img.getpixel((1, 0)) == (0, 0, 255)


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


def test_list_monitors_keeps_a_negative_origin_and_the_primary_flag(monkeypatch):
    _fake_win32(
        monkeypatch,
        monitors=[((0, 0, 1920, 1080), 1), ((0, -1080, 1920, 0), 0)],
    )
    monitors = desktop_capture.list_monitors()
    assert monitors == [
        Monitor(id=0, left=0, top=0, width=1920, height=1080, primary=True),
        Monitor(id=1, left=0, top=-1080, width=1920, height=1080, primary=False),
    ]


def test_grab_blits_only_the_monitors_own_rect(monkeypatch):
    calls = _fake_win32(monkeypatch)
    above = Monitor(id=1, left=-8, top=-1080, width=4, height=3, primary=False)
    img = desktop_capture.grab_monitor(above)
    assert calls["blit"] == [((0, 0), (4, 3), (-8, -1080))]
    assert img.size == (4, 3) and img.getpixel((0, 0)) == (255, 0, 0)
    assert calls["released"] == 1 and calls["deleted"] == 1


def test_a_failed_blit_is_capture_unavailable_and_still_cleans_up(monkeypatch):
    calls = _fake_win32(monkeypatch, blit_error=True)
    with pytest.raises(CaptureUnavailable):
        desktop_capture.grab_monitor(PRIMARY)
    assert calls["released"] == 1 and calls["deleted"] == 1
