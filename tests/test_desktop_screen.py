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

The cursor (#1598): :mod:`src.desktop_cursor` maps the PC's cursor to a shape
name and a 0-1 spot on the shown monitor, and its loop sends only changes,
stops with the socket, and backs off while the cursor can't be read. Route
tests read it through a fake reader, never the real cursor.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
import types
from pathlib import Path
from typing import Dict, List

import pytest
from PIL import Image
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src import desktop_capture, desktop_cursor
from src.desktop_capture import CaptureUnavailable, Monitor
from src.desktop_cursor import CursorState, CursorUnavailable
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
def fake_cursor(monkeypatch) -> Dict:
    """The route's cursor reader, faked: ``readers`` records the monitor each
    socket built one for, ``reads`` counts reads, ``state`` is what they
    return. No test reads the real cursor."""
    cursor: Dict = {"readers": [], "reads": 0,
                    "state": CursorState(visible=True, x=0.5, y=0.25, shape="text")}

    def reader(monitor: Monitor):
        cursor["readers"].append(monitor)

        def read() -> CursorState:
            cursor["reads"] += 1
            return cursor["state"]
        return read

    monkeypatch.setattr(desktop_cursor, "cursor_reader", reader)
    return cursor


@pytest.fixture
def screen(webapp_client, monkeypatch, tmp_path: Path, fake_cursor):
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
        frame = _next_frame(ws)
    assert frame[:2] == b"\xff\xd8", "a JPEG frame"
    assert grabs and grabs[0] == PRIMARY, "no monitor asked for: the primary"


def test_ws_streams_to_the_pc_itself_without_credentials(screen):
    app, _, _ = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        assert _next_frame(ws)[:2] == b"\xff\xd8"


def test_ws_selects_the_monitor_above_the_primary(screen):
    app, _, grabs = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url(monitor="1")) as ws:
        frame = _next_frame(ws)
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
        frame = _next_frame(ws)
        closed = _close_and_wait(ws, audit)
    events = [c.args[0] for c in audit.audit_event.call_args_list]
    assert events == ["screen_open", "screen_close"]
    assert closed["client"] == LOOPBACK and closed["monitor"] == 0
    assert closed["frames"] == 1 and closed["duration_s"] >= 0
    assert closed["bytes"] == len(frame), "the close line carries the traffic sent"
    assert closed["unavailable"] == 0


def test_ws_a_blocked_capture_says_so_and_is_audited(screen, webapp_client, monkeypatch):
    """A desktop that can't be read (locked, or capture blocked by endpoint
    security) is a status message to the viewer and a count on the close
    line: the durable trace, since the tray discards the webapp's own log
    (#1688)."""
    app, _, _ = screen
    audit = webapp_client[2]["audit"]

    def blocked(_monitor):
        raise CaptureUnavailable("BitBlt failed")

    monkeypatch.setattr(desktop_capture, "grab_monitor", blocked)
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        assert _next_text(ws, "status") == {"type": "status", "state": "unavailable"}
        closed = _close_and_wait(ws, audit)
    assert closed["frames"] == 0 and closed["bytes"] == 0
    assert closed["unavailable"] == 1


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


@pytest.fixture
def direct_screen_ws(monkeypatch, fake_cursor):
    """``screen_ws`` driven on a bare event loop with a scripted capture loop,
    for the tests that need to cancel the handler at an exact await. Yields
    ``(run, audited)``: ``run(script, on_close)`` plays one session where
    ``script(send_frame, send_status)`` stands in for the capture loop and
    ``on_close`` is awaited when the close line is written; ``audited`` is the
    list of ``(event, fields)`` that finished writing."""
    from app.webapp.routers import desktop_screen

    audited: List = []

    async def fake_audit(write, event, **fields):
        if event == "screen_close":
            await run_state["on_close"]()
        audited.append((event, fields))

    run_state: Dict = {}

    async def fake_stream_frames(_grab, send_frame, send_status, _acked, _stopped, **_kwargs):
        await run_state["script"](send_frame, send_status)

    monkeypatch.setattr(desktop_screen, "audit_off_loop", fake_audit)
    monkeypatch.setattr(desktop_screen, "ws_terminal_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(desktop_screen, "client_ip_ws", lambda _ws: LOOPBACK)
    monkeypatch.setattr(desktop_capture, "list_monitors", lambda: [PRIMARY])
    monkeypatch.setattr(desktop_capture, "stream_frames", fake_stream_frames)

    def run(script, on_close):
        run_state["script"] = script
        run_state["on_close"] = on_close
        return desktop_screen.screen_ws(_SilentSocket())

    return run, audited


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
        await desktop_screen._read_client(_ScriptedSocket(["ack"]), acked, stopped)
        return acked.is_set(), stopped.is_set()

    assert _run(go()) == (True, True)


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


@pytest.mark.parametrize(
    "host, token, with_tt, headers, code",
    [
        (TAILNET, TOKEN, True, {"cf-ray": "abc-AMS"}, 4403),   # the public edge
        (LOOPBACK, "", False, {"cf-ray": "abc-AMS"}, 4403),    # edge over loopback
        (LAN, TOKEN, True, None, 4403),                         # off the tailnet
        (TAILNET, "", True, None, 4401),                        # no bearer
        (TAILNET, TOKEN, False, None, 4401),                    # no passkey unlock
    ],
    ids=["cloudflare", "cloudflare-loopback", "off-tailnet", "no-bearer", "no-passkey"],
)
def test_a_refused_socket_injects_nothing(
    screen, fake_injector, fake_cursor, host, token, with_tt, headers, code
):
    app, tt, grabs = screen
    url = _ws_url(token, tt if with_tt else "")
    assert _ws_input_refused(_as(app, host), url, headers) == code
    time.sleep(0.1)  # give a (wrongly) running reader every chance to act
    assert fake_injector.batches == [], "a refused socket's input reached the injector"
    assert grabs == []
    assert fake_cursor["readers"] == [] and fake_cursor["reads"] == 0, (
        "a refused socket read the cursor"
    )


def test_a_forged_passkey_token_injects_nothing(screen, fake_injector):
    app, _, _ = screen
    assert _ws_input_refused(_as(app, TAILNET), _ws_url(TOKEN, "not-minted")) == 4401
    time.sleep(0.1)
    assert fake_injector.batches == []


def test_an_unlocked_tailnet_client_clicks_where_it_taps(screen, fake_injector):
    from src import desktop_input as di

    app, tt, _ = screen
    with _as(app, TAILNET).websocket_connect(_ws_url(TOKEN, tt)) as ws:
        _next_frame(ws)
        ws.send_text(_CLICK)
        _wait_for(lambda: fake_injector.batches, "the click")
    # The fixture's desktop: PRIMARY (64x36 at 0,0) under ABOVE (0,-36), so
    # the virtual desktop is 64x72 at (0,-36); the primary's centre pixel
    # (32, 18) is (32, 54) into it.
    move, down, up = fake_injector.batches[0]
    assert (move.dx, move.dy) == (round(32 * 65535 / 63), round(54 * 65535 / 71))
    assert move.flags & di.MOUSEEVENTF_ABSOLUTE and move.flags & di.MOUSEEVENTF_VIRTUALDESK
    assert (down.flags, up.flags) == (di.MOUSEEVENTF_LEFTDOWN, di.MOUSEEVENTF_LEFTUP)


def test_input_maps_onto_the_monitor_being_viewed(screen, fake_injector):
    app, _, _ = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url(monitor="1")) as ws:
        _next_frame(ws)
        ws.send_text(json.dumps({"type": "click", "x": 0, "y": 0}))
        _wait_for(lambda: fake_injector.batches, "the click")
    move = fake_injector.batches[0][0]
    assert (move.dx, move.dy) == (0, 0), "ABOVE's top-left is the desktop's"


def test_disconnect_releases_a_held_button(screen, fake_injector, webapp_client):
    from src import desktop_input as di

    app, _, _ = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        _next_frame(ws)
        ws.send_text(_DOWN)
        _wait_for(lambda: fake_injector.batches, "the press")
    # The phone went away mid-drag. Leaving the block this way cancels the
    # handler (TestClient does; anyio then re-cancels every later await), the
    # harshest end a socket can have: the release is the first, synchronous
    # step of the handler's finally, so it still runs.
    _wait_for(lambda: len(fake_injector.batches) == 2, "the release")
    assert fake_injector.batches[-1] == [di.MouseEvent(di.MOUSEEVENTF_LEFTUP)]


def test_the_close_audit_counts_inputs_and_never_logs_what_was_typed(
    screen, fake_injector, webapp_client
):
    app, _, _ = screen
    audit = webapp_client[2]["audit"]
    secret = "hunter2-ñ€"
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        _next_frame(ws)
        ws.send_text(json.dumps({"type": "text", "text": secret}))
        ws.send_text(json.dumps({"type": "key", "key": "c", "mods": ["ctrl"]}))
        ws.send_text("not an input message")
        _wait_for(lambda: len(fake_injector.batches) == 2, "both inputs")
        closed = _close_and_wait(ws, audit)
    assert closed["inputs"] == 2
    for call in audit.audit_event.call_args_list:
        assert secret not in repr(call), "typed text reached the audit log"


# ------------------------------------------------------ clipboard (#1719)
#
# Every test here runs against tests/conftest.py's recording fake clipboard:
# nothing reaches the real one.


def _clipboard_frame(text: str) -> str:
    return json.dumps({"type": "clipboard_set", "text": text}, separators=(",", ":"), ensure_ascii=False)


def test_a_clipboard_set_on_the_gated_socket_lands_and_is_answered(screen, fake_clipboard):
    app, tt, _ = screen
    secret = "hunter2-ñ€ correct horse"
    with _as(app, TAILNET).websocket_connect(_ws_url(TOKEN, tt)) as ws:
        _next_frame(ws)
        ws.send_text(_clipboard_frame(secret))
        reply = _next_text(ws, "clipboard")
    assert fake_clipboard.texts == [secret]
    assert reply == {"type": "clipboard", "direction": "to_pc", "ok": True}
    assert secret not in json.dumps(reply)


def test_a_clipboard_set_over_the_cap_is_answered_and_copies_nothing(screen, fake_clipboard):
    from src import desktop_clipboard

    app, _, _ = screen
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        _next_frame(ws)
        ws.send_text(_clipboard_frame("a" * (desktop_clipboard.MAX_CLIPBOARD_BYTES + 1)))
        reply = _next_text(ws, "clipboard")
    assert reply == {"type": "clipboard", "direction": "to_pc", "ok": False, "reason": "too_large"}
    assert fake_clipboard.texts == []


@pytest.mark.parametrize(
    "host, token, with_tt, headers, code",
    [
        (TAILNET, TOKEN, True, {"cf-ray": "abc-AMS"}, 4403),   # the public edge
        (LAN, TOKEN, True, None, 4403),                         # off the tailnet
        (TAILNET, "", True, None, 4401),                        # no bearer
        (TAILNET, TOKEN, False, None, 4401),                    # no passkey unlock
    ],
    ids=["cloudflare", "off-tailnet", "no-bearer", "no-passkey"],
)
def test_a_refused_socket_copies_nothing(screen, fake_clipboard, host, token, with_tt, headers, code):
    app, tt, _ = screen
    url = _ws_url(token, tt if with_tt else "")
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with _as(app, host).websocket_connect(url, headers=headers or {}) as ws:
            ws.send_text(_clipboard_frame("never copied"))
            _next_frame(ws)
    assert excinfo.value.code == code
    time.sleep(0.1)  # give a (wrongly) running reader every chance to act
    assert fake_clipboard.texts == [], "a refused socket's clipboard text reached the clipboard"


def test_the_close_audit_counts_clipboard_transfers_and_never_logs_what_was_copied(
    screen, fake_clipboard, webapp_client
):
    app, _, _ = screen
    audit = webapp_client[2]["audit"]
    secret = "hunter2-ñ€ correct horse"
    with _as(app, LOOPBACK).websocket_connect(_ws_url()) as ws:
        _next_frame(ws)
        ws.send_text(_clipboard_frame(secret))
        _next_text(ws, "clipboard")
        ws.send_text(_clipboard_frame(secret))
        _next_text(ws, "clipboard")
        closed = _close_and_wait(ws, audit)
    assert closed["clipboard_to_pc"] == 2
    assert closed["inputs"] == 0, "a clipboard transfer is not an input message"
    for call in audit.audit_event.call_args_list:
        assert secret not in repr(call), "copied text reached the audit log"
    # Direction only: the transfers are a count, with no size field beside it.
    assert not [k for k in closed if "size" in k or "len" in k or "chars" in k or "bytes_copied" in k]


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


# ------------------------------------------------------- cursor (#1598)


def test_standard_cursors_map_to_shape_names_and_anything_else_is_an_arrow():
    # A fake user32 whose shared cursor handle is the IDC id plus 1000.
    fake = types.SimpleNamespace(LoadCursorW=lambda _inst, idc: idc.value + 1000)
    table = desktop_cursor.standard_cursors(fake)
    assert desktop_cursor.shape_for(32512 + 1000, table) == "arrow"
    assert desktop_cursor.shape_for(32513 + 1000, table) == "text"
    assert desktop_cursor.shape_for(32649 + 1000, table) == "hand"
    assert desktop_cursor.shape_for(32514 + 1000, table) == "busy"
    assert desktop_cursor.shape_for(32650 + 1000, table) == "busy", "app-starting reads as busy"
    for idc, name in ((32645, "resize-ns"), (32644, "resize-ew"), (32642, "resize-nwse"),
                      (32643, "resize-nesw"), (32646, "move"), (32648, "not-allowed")):
        assert desktop_cursor.shape_for(idc + 1000, table) == name
    # An app's own cursor, and no cursor handle at all.
    assert desktop_cursor.shape_for(0xBEEF, table) == "arrow"
    assert desktop_cursor.shape_for(None, table) == "arrow"
    assert set(table.values()) == desktop_cursor.SHAPES


def test_the_client_draws_every_shape_the_server_sends():
    """screen-cursor.js has a drawing for each name; a missing one would
    silently draw the arrow."""
    js = (Path(__file__).resolve().parent.parent
          / "app" / "webapp" / "static" / "screen-cursor.js").read_text(encoding="utf-8")
    for name in desktop_cursor.SHAPES:
        assert f"'{name}':" in js, name


def test_cursor_fraction_is_the_pixel_centre_within_the_monitor():
    wide = Monitor(id=0, left=0, top=0, width=1920, height=1080, primary=True)
    # The centre of pixel (959, 539): an even width has no pixel at 0.5.
    assert desktop_cursor.cursor_fraction(wide, 959, 539) == (0.4997, 0.4995)
    assert desktop_cursor.cursor_fraction(wide, 0, 0) == (round(0.5 / 1920, 4), round(0.5 / 1080, 4))
    last = desktop_cursor.cursor_fraction(wide, 1919, 1079)
    assert last is not None and last[0] < 1 and last[1] < 1
    # The next pixel is another monitor's.
    assert desktop_cursor.cursor_fraction(wide, 1920, 10) is None
    assert desktop_cursor.cursor_fraction(wide, -1, 10) is None
    assert desktop_cursor.cursor_fraction(wide, 10, 1080) is None
    # A monitor above the primary: a negative origin.
    above = Monitor(id=1, left=0, top=-1080, width=1920, height=1080, primary=False)
    assert desktop_cursor.cursor_fraction(above, 959, -541) == (0.4997, 0.4995)
    assert desktop_cursor.cursor_fraction(above, 959, 0) is None


@pytest.mark.parametrize("fx, fy", [(0.0, 0.0), (0.25, 0.75), (0.5, 0.5), (0.9999, 1.0), (1.0, 0.33)])
def test_a_tapped_spot_comes_back_as_the_cursor_spot(fx, fy):
    """A tap moves the real cursor to map_point's pixel; reading the cursor
    there gives the tapped fraction back within one pixel, so the drawn
    cursor lands where the tap did."""
    from src import desktop_input

    above = Monitor(id=1, left=-8, top=-1080, width=1920, height=1080, primary=False)
    px = above.left + min(above.width - 1, int(fx * above.width))
    py = above.top + min(above.height - 1, int(fy * above.height))
    # The same pixel map_point aims at (its absolute units are a function of it).
    desktop = desktop_input.virtual_desktop([above])
    assert desktop_input.map_point(above, desktop, fx, fy) == (
        round((px - desktop[0]) * 65535 / (desktop[2] - 1)),
        round((py - desktop[1]) * 65535 / (desktop[3] - 1)),
    )
    back = desktop_cursor.cursor_fraction(above, px, py)
    assert back is not None
    assert abs(back[0] - fx) <= 1 / above.width and abs(back[1] - fy) <= 1 / above.height


def test_a_hidden_cursor_sends_no_position_or_shape():
    mon = Monitor(id=0, left=0, top=0, width=100, height=100, primary=True)
    showing = desktop_cursor.cursor_state(mon, True, 7, 49, 49, {7: "hand"})
    assert showing.to_message() == {
        "type": "cursor", "x": 0.495, "y": 0.495, "shape": "hand", "visible": True,
    }
    not_showing = desktop_cursor.cursor_state(mon, False, 7, 49, 49, {7: "hand"})
    off_monitor = desktop_cursor.cursor_state(mon, True, 7, 500, 49, {7: "hand"})
    assert not_showing.to_message() == off_monitor.to_message() == {"type": "cursor", "visible": False}


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


def test_the_reader_reads_position_shape_and_showing_flag(monkeypatch):
    _fake_cursor_win32(monkeypatch, [
        (1, 1, 32513 + 1000, 959, 539),   # an I-beam, showing
        (1, 0, 0, 959, 539),              # hidden
        (1, 2, 32512 + 1000, 959, 539),   # suppressed (touch on the PC): hidden
        (0, 0, 0, 0, 0),                  # the secure desktop
    ])
    read = desktop_cursor.cursor_reader(
        Monitor(id=0, left=0, top=0, width=1920, height=1080, primary=True)
    )
    assert read() == CursorState(visible=True, x=0.4997, y=0.4995, shape="text")
    assert read() == desktop_cursor.HIDDEN
    assert read() == desktop_cursor.HIDDEN
    with pytest.raises(CursorUnavailable):
        read()


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


def test_a_still_cursor_is_sent_once_and_a_move_is_sent():
    reads = {"n": 0}

    def read() -> CursorState:
        reads["n"] += 1
        x = 0.25 if reads["n"] < 4 else 0.75
        return CursorState(visible=True, x=x, y=0.5)

    sent = _run(_drive_cursor(read, run_for=0.3))
    assert reads["n"] > 6, "the loop kept looking"
    assert [m["x"] for m in sent] == [0.25, 0.75]


def test_an_unreadable_cursor_is_hidden_once_logged_once_and_not_polled_fast(caplog):
    reads = {"n": 0}

    def read() -> CursorState:
        reads["n"] += 1
        raise CursorUnavailable("secure desktop")

    with caplog.at_level("INFO", logger="src.desktop_cursor"):
        sent = _run(_drive_cursor(read, run_for=0.3, interval=0.001, retry=0.1))
    assert sent == [{"type": "cursor", "visible": False}]
    assert reads["n"] <= 4, "an unreadable cursor must back off to the retry pace"
    assert len([r for r in caplog.records if "cursor unreadable" in r.getMessage()]) == 1


def test_the_cursor_comes_back_after_it_was_unreadable():
    reads = {"n": 0}

    def read() -> CursorState:
        reads["n"] += 1
        if reads["n"] <= 3:
            raise CursorUnavailable("locked")
        return CursorState(visible=True, x=0.5, y=0.5)

    sent = _run(_drive_cursor(read, run_for=0.2))
    assert sent == [
        {"type": "cursor", "visible": False},
        {"type": "cursor", "x": 0.5, "y": 0.5, "shape": "arrow", "visible": True},
    ]


def test_the_cursor_loop_ends_when_stopped():
    async def go():
        stopped = asyncio.Event()
        stopped.set()

        async def send(_message):
            raise AssertionError("nothing may be sent once stopped")

        return await desktop_cursor.stream_cursor(
            lambda: CursorState(visible=True, x=0.1, y=0.1), send, stopped
        )

    assert _run(go()) == 0


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
