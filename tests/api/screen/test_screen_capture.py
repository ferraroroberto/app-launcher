"""Screen capture: frame pacing, downscaling, monitor choice, the cursor reader and the Win32 grab."""

from __future__ import annotations

import asyncio
import io
import types
from pathlib import Path

import pytest
from PIL import Image

from src import desktop_capture, desktop_cursor
from src.desktop_capture import CaptureUnavailable, Monitor
from src.desktop_cursor import CursorState, CursorUnavailable

from tests.api.screen._support import (
    ABOVE,
    PRIMARY,
    _drive,
    _drive_cursor,
    _fake_cursor_win32,
    _fake_win32,
    _run,
    encoded,
)


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
    with Image.open(io.BytesIO(encoded(big, 100))) as img:
        assert img.size == (100, 50)
    with Image.open(io.BytesIO(encoded(big, 800))) as img:
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
    js = (Path(__file__).resolve().parents[3]
          / "app" / "webapp" / "static" / "screen" / "screen-cursor.js").read_text(encoding="utf-8")
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
