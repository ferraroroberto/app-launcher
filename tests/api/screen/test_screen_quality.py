"""Screen view quality presets and the live readout (#1721).

Three presets (sharp, balanced, saver) live in one table in
:mod:`src.desktop_capture`; the connect query's ``preset=`` picks one, and an
unknown value is Balanced. These tests pin each preset's numbers (JPEG quality,
pause between frames, width cap), that the route hands them to the capture loop
and the audit line, and the readout's server half: per-stage times in a
``stats`` message about once a second, only while the client has switched the
readout on, with the switch kept out of the input path.

Everything runs against fake grabbers, a fake cursor and the recording fakes
for input and clipboard (tests/conftest.py): nothing captures or drives the
real desktop.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from typing import Any, Dict, List

import pytest
from PIL import Image, ImageDraw

from src import desktop_capture
from src.desktop_capture import Monitor, StageStats

from tests.api.screen._support import (
    LOOPBACK,
    _as,
    _close_and_wait,
    _next_frame,
    _next_text,
    _wait_for,
    encoded,
)

BIG = Monitor(id=0, left=0, top=0, width=1920, height=1080, primary=True)


def _picture(width: int = 1920, height: int = 1080) -> Image.Image:
    """Something with detail, so a lower quality really is a smaller file."""
    img = Image.new("RGB", (width, height), (30, 60, 120))
    draw = ImageDraw.Draw(img)
    for i in range(0, width, 24):
        draw.line([(i, 0), (width - i, height)], fill=(i % 256, (i * 7) % 256, 200), width=3)
    return img


def _url(preset: str = "", width: int = 1920) -> str:
    query = [f"width={width}"]
    if preset:
        query.append(f"preset={preset}")
    return "/api/screen/ws?" + "&".join(query)


# ------------------------------------------------------------- the table


def test_each_preset_has_its_pinned_numbers():
    presets = desktop_capture.PRESETS
    assert set(presets) == {"sharp", "balanced", "saver"}
    assert (presets["sharp"].quality, presets["sharp"].interval, presets["sharp"].max_width) == (
        85, 0.15, None,
    )
    assert (presets["balanced"].quality, presets["balanced"].interval, presets["balanced"].max_width) == (
        70, 0.15, None,
    )
    assert (presets["saver"].quality, presets["saver"].interval, presets["saver"].max_width) == (
        50, 0.3, 960,
    )


def test_balanced_is_what_the_screen_view_did_before_presets():
    balanced = desktop_capture.PRESETS[desktop_capture.DEFAULT_PRESET]
    assert desktop_capture.DEFAULT_PRESET == "balanced"
    assert balanced.quality == desktop_capture.JPEG_QUALITY == 70
    assert balanced.interval == desktop_capture.FRAME_INTERVAL_S == 0.15
    assert balanced.max_width is None


@pytest.mark.parametrize("asked", [None, "", "  ", "nope", "SHARP2", "0", "../etc"])
def test_an_unknown_preset_falls_back_to_balanced(asked):
    assert desktop_capture.pick_preset(asked).name == "balanced"


@pytest.mark.parametrize("asked, name", [("sharp", "sharp"), (" Saver ", "saver"), ("BALANCED", "balanced")])
def test_a_known_preset_is_found_whatever_its_case(asked, name):
    assert desktop_capture.pick_preset(asked).name == name


def test_the_presets_cut_the_frame_size_in_order():
    picture = _picture()
    sizes = {
        name: len(encoded(picture, desktop_capture.frame_width(1920, 1920, preset), preset.quality))
        for name, preset in desktop_capture.PRESETS.items()
    }
    assert sizes["sharp"] > sizes["balanced"] > sizes["saver"], sizes


def test_the_width_cap_only_ever_lowers_the_width():
    sharp = desktop_capture.PRESETS["sharp"]
    saver = desktop_capture.PRESETS["saver"]
    assert desktop_capture.frame_width(1920, 1920, sharp) == 1920
    assert desktop_capture.frame_width(1920, 1920, saver) == 960
    # A phone that asked for less, or a monitor that is narrower, keeps that.
    assert desktop_capture.frame_width(800, 1920, saver) == 800
    assert desktop_capture.frame_width(1920, 640, saver) == 640
    assert desktop_capture.frame_width(None, 1920, saver) == 960


def test_a_frame_is_encoded_at_the_quality_it_is_given():
    picture = _picture(640, 360)
    low = encoded(picture, 640, 50)
    high = encoded(picture, 640, 85)
    assert len(high) > len(low)
    # No quality given is today's.
    assert encoded(picture, 640) == encoded(picture, 640, 70)


# ------------------------------------------------------ the capture loop


async def _stream(grab, *, width=640, quality=70, interval=0.001, run_for=0.15, stats=None):
    acked, stopped = asyncio.Event(), asyncio.Event()
    acked.set()
    frames: List[bytes] = []

    async def send_frame(data: bytes) -> None:
        frames.append(data)
        acked.set()

    async def send_status(_state: str) -> None:
        pass

    task = asyncio.create_task(desktop_capture.stream_frames(
        grab, send_frame, send_status, acked, stopped,
        width=width, interval=interval, quality=quality, stats=stats,
    ))
    await asyncio.sleep(run_for)
    stopped.set()
    await asyncio.wait_for(task, timeout=2)
    return frames


@pytest.mark.parametrize("name", ["sharp", "balanced", "saver"])
def test_the_loop_encodes_every_frame_at_the_presets_quality(name):
    preset = desktop_capture.PRESETS[name]
    picture = _picture(640, 360)
    frames = asyncio.run(_stream(lambda: picture, quality=preset.quality))
    assert frames, "no frame was sent"
    assert frames[0] == encoded(picture, 640, preset.quality)


def test_the_loop_paces_frames_by_the_interval_it_is_given():
    shade = iter(range(100000))

    def changing():
        return Image.new("RGB", (64, 36), (next(shade) % 256, 0, 0))

    fast = asyncio.run(_stream(changing, width=64, interval=0.02, run_for=0.5))
    slow = asyncio.run(_stream(changing, width=64, interval=0.2, run_for=0.5))
    assert len(fast) > 2 * len(slow) >= 2, (len(fast), len(slow))


def test_stage_times_are_recorded_per_stage():
    picture = _picture(640, 360)

    def slow_grab():
        time.sleep(0.03)
        return picture

    stats = StageStats()
    asyncio.run(_stream(slow_grab, width=320, stats=stats, run_for=0.3))
    message = stats.take()
    assert message["type"] == "stats"
    assert message["grab"] >= 30, message
    for stage in ("digest", "resize", "encode"):
        assert message[stage] is not None and message[stage] >= 0, message
    assert message["frames"] == 1, "a still picture is encoded once"
    # The period is over: the next one starts empty.
    assert stats.take() is None


def test_a_loop_without_stats_still_streams():
    picture = _picture(320, 180)
    assert asyncio.run(_stream(lambda: picture, width=320, stats=None))


def test_stage_stats_are_means_in_milliseconds_and_none_for_a_stage_that_did_not_run():
    stats = StageStats()
    assert stats.take() is None
    stats.record("grab", 0.010)
    stats.record("grab", 0.030)
    stats.record("digest", 0.0024)
    message = stats.take()
    assert message == {
        "type": "stats", "grab": 20.0, "digest": 2.4, "resize": None, "encode": None, "frames": 0,
    }
    assert stats.take() is None, "taking starts a new period"


# --------------------------------------------------------- the readout switch


@pytest.mark.parametrize("text, expected", [
    ('{"type":"readout","on":true}', True),
    ('{"type": "readout", "on": false}', False),
    ('{"type":"readout","on":"yes"}', False),
    ('{"type":"readout","on":1}', False),
    ('{"type":"readout"}', False),
    ('{"type":"click","x":0.5,"y":0.5}', None),
    ('{"type":"text","text":"readout"}', None),
    ("ack", None),
    ("not json readout", None),
    ('[1,"readout"]', None),
    ('{"type":"readout","on":true,"pad":"' + "x" * 80 + '"}', None),
])
def test_only_a_small_readout_message_is_the_switch(text, expected):
    assert desktop_capture.parse_readout(text) is expected


async def _run_stats(readout_on: bool, run_for: float, monkeypatch) -> List[Dict[str, Any]]:
    from app.webapp.routers import desktop_screen

    monkeypatch.setattr(desktop_capture, "STATS_INTERVAL_S", 0.03)
    stats = StageStats()
    readout, stopped = asyncio.Event(), asyncio.Event()
    if readout_on:
        readout.set()
    sent: List[Dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    async def feed():
        # The capture loop keeps working the whole time, switch or no switch.
        while not stopped.is_set():
            stats.record("grab", 0.004)
            await asyncio.sleep(0.005)

    task = asyncio.create_task(desktop_screen._stream_stats(stats, readout, send, stopped))
    feeder = asyncio.create_task(feed())
    await asyncio.sleep(run_for)
    stopped.set()
    await asyncio.gather(task, feeder)
    return sent


def test_with_the_readout_off_no_stats_message_is_sent(monkeypatch):
    sent = asyncio.run(_run_stats(False, 0.3, monkeypatch))
    assert sent == []


def test_with_the_readout_on_stats_go_out_about_once_per_interval(monkeypatch):
    sent = asyncio.run(_run_stats(True, 0.3, monkeypatch))
    assert len(sent) >= 3, sent
    assert all(m["type"] == "stats" and m["grab"] == 4.0 for m in sent)


# ------------------------------------------------------------ on the socket


@pytest.fixture
def big_screen(screen, monkeypatch):
    """The ``screen`` app, but with one 1920x1080 monitor and a real picture,
    and the stats period shortened. ``screen``'s own fake grabber is replaced."""
    app, tt, grabs = screen
    picture = _picture()

    def grab(monitor: Monitor) -> Image.Image:
        grabs.append(monitor)
        return picture

    monkeypatch.setattr(desktop_capture, "list_monitors", lambda: [BIG])
    monkeypatch.setattr(desktop_capture, "grab_monitor", grab)
    monkeypatch.setattr(desktop_capture, "STATS_INTERVAL_S", 0.05)
    return app, tt, grabs, picture


@pytest.mark.parametrize("name", ["sharp", "balanced", "saver"])
def test_the_route_sends_frames_at_the_presets_quality_and_width(big_screen, name):
    app, _, _, picture = big_screen
    preset = desktop_capture.PRESETS[name]
    with _as(app, LOOPBACK).websocket_connect(_url(name)) as ws:
        frame = _next_frame(ws)
    width = desktop_capture.frame_width(1920, 1920, preset)
    assert frame == encoded(picture, width, preset.quality)
    with Image.open(io.BytesIO(frame)) as img:
        assert img.width == (960 if name == "saver" else 1920)


def test_no_preset_and_an_unknown_preset_both_get_balanced(big_screen):
    app, _, _, picture = big_screen
    expected = encoded(picture, 1920, 70)
    for url in (_url(), _url("bogus")):
        with _as(app, LOOPBACK).websocket_connect(url) as ws:
            assert _next_frame(ws) == expected


@pytest.mark.parametrize("name", ["sharp", "balanced", "saver"])
def test_the_route_hands_the_presets_pace_and_quality_to_the_loop(big_screen, monkeypatch, name):
    app, _, _, _ = big_screen
    seen: Dict[str, Any] = {}

    async def fake_stream_frames(_grab, send_frame, _send_status, _acked, stopped, **kwargs):
        seen.update(kwargs)
        await send_frame(b"x")
        await stopped.wait()

    monkeypatch.setattr(desktop_capture, "stream_frames", fake_stream_frames)
    with _as(app, LOOPBACK).websocket_connect(_url(name)) as ws:
        _next_frame(ws)
    preset = desktop_capture.PRESETS[name]
    assert seen["interval"] == preset.interval
    assert seen["quality"] == preset.quality
    assert seen["width"] == (960 if name == "saver" else 1920)
    assert isinstance(seen["stats"], StageStats)


@pytest.mark.parametrize("asked, recorded", [
    ("saver", "saver"), ("sharp", "sharp"), ("balanced", "balanced"), ("", "balanced"), ("zzz", "balanced"),
])
def test_screen_open_records_the_preset_that_applies(big_screen, webapp_client, asked, recorded):
    app, _, _, _ = big_screen
    audit = webapp_client[2]["audit"]
    with _as(app, LOOPBACK).websocket_connect(_url(asked)) as ws:
        _next_frame(ws)
        _close_and_wait(ws, audit)
    opened = [c for c in audit.audit_event.call_args_list if c.args[0] == "screen_open"]
    assert len(opened) == 1
    assert opened[0].kwargs["preset"] == recorded
    # No content, only the numbers of the connection.
    assert set(opened[0].kwargs) == {"client", "monitor", "rect", "width", "preset"}


def test_no_stats_until_the_readout_is_switched_on_and_the_switch_is_not_input(
    big_screen, webapp_client, fake_injector
):
    app, _, grabs, _ = big_screen
    audit = webapp_client[2]["audit"]
    with _as(app, LOOPBACK).websocket_connect(_url()) as ws:
        _next_frame(ws)
        ws.send_text("ack")
        # Several stats periods pass with captures going on and the readout off.
        _wait_for(lambda: len(grabs) >= 4, "a few idle captures")
        time.sleep(0.2)
        # A reply to a message sent now arrives after anything already queued.
        ws.send_text(json.dumps({"type": "clipboard_set", "text": "probe"}))
        seen: List[str] = []
        while True:
            message = ws.receive()
            if message.get("text") is None:
                continue
            kind = json.loads(message["text"]).get("type")
            seen.append(kind)
            if kind == "clipboard":
                break
        assert "stats" not in seen, seen

        ws.send_text(json.dumps({"type": "readout", "on": True}))
        stats = _next_text(ws, "stats")
        assert stats["grab"] is not None and stats["grab"] >= 0
        assert set(stats) == {"type", "grab", "digest", "resize", "encode", "frames"}
        closed = _close_and_wait(ws, audit)
    assert closed["inputs"] == 0, "the readout switch is not an input message"
    assert fake_injector.batches == []
