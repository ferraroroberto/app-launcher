"""Screen input and clipboard: what a gated socket injects, and what the close audit records."""

from __future__ import annotations

import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect


from tests.api.screen._support import (
    LAN,
    LOOPBACK,
    TAILNET,
    TOKEN,
    _CLICK,
    _DOWN,
    _as,
    _clipboard_frame,
    _close_and_wait,
    _next_frame,
    _next_text,
    _wait_for,
    _ws_input_refused,
    _ws_url,
)


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
    from src.desktop import input as di

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
    from src.desktop import input as di

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
    from src.desktop import clipboard as desktop_clipboard

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
