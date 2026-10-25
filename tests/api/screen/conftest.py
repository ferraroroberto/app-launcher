"""Fixtures for the Screen view tests: a fake capture and cursor behind the real route."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest
from PIL import Image

from src.desktop import capture as desktop_capture
from src.desktop import cursor as desktop_cursor
from src.desktop.capture import Monitor
from src.desktop.cursor import CursorState
from src.auth.webauthn_gate import WebAuthnGate

from tests.api.screen._support import ABOVE, LOOPBACK, PRIMARY, TOKEN, _SilentSocket


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
