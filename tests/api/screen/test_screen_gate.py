"""The Screen view's gate: every path is passkey-class, and the monitors list and the stream refuse what they must."""

from __future__ import annotations

import io

from PIL import Image

from src.desktop import capture as desktop_capture
from src.desktop.capture import CaptureUnavailable

from tests.api.screen._support import (
    ABOVE,
    LAN,
    LOOPBACK,
    PRIMARY,
    TAILNET,
    TOKEN,
    _as,
    _close_and_wait,
    _next_frame,
    _next_text,
    _ws_close_code,
    _ws_url,
)


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
