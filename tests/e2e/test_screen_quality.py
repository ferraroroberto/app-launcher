"""Regression pin for #1721: the Screen view's quality presets and live readout.

A Quality button in the tools row opens a small sheet: three presets (Sharp,
Balanced, Data saver) and a readout switch. The preset rides the connect query
(``preset=``), is remembered per device, and is applied on the next open and on
a reconnect; a pick reconnects. The readout is an overlay of fps, kB/s, average
frame size and ack-to-next-frame time worked out from the frames the page
actually received, plus the PC's stage times from the server's ``stats``
message. Switching it on sends one small ``readout`` message (not input); with
it off nothing is sent and nothing is shown.

Nothing here touches the real screen: the monitor list and the socket are the
stub ``test_screen_view`` uses, the frames are invented JPEGs of a known size,
and ``performance.now`` is pinned by the test, so the readout's arithmetic is
checked against exact bytes and exact milliseconds. The server half (the preset
numbers, the stage times, no ``stats`` while off) is
``tests/api/screen/test_screen_quality.py``; the Node-side arithmetic is
``tests/js/screen_quality.test.mjs``.
"""
from __future__ import annotations

import io
import json
import random
import re
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e._surface import surface
from tests.e2e.conftest import wait_until
from tests.e2e.test_screen_view import (
    _frame,
    _open_from_sheet,
    _set_mode,
    _stub_screen,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# performance.now() reads window.__t once the test sets it, and the real clock
# before: the readout's windows are then exact, whatever the box is doing.
_PIN_CLOCK = """
(() => {
  const real = performance.now.bind(performance);
  performance.now = () => (window.__t === undefined ? real() : window.__t);
})()
"""

_READOUT_ON = {"type": "readout", "on": True}
_READOUT_OFF = {"type": "readout", "on": False}


def _big_frame() -> bytes:
    """A JPEG of a few kB, always the same bytes: the readout needs a size
    worth rounding, and a size the test knows."""
    rng = random.Random(1721)
    img = Image.frombytes("RGB", (80, 45), bytes(rng.randrange(256) for _ in range(80 * 45 * 3)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _preset_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["preset"][0]


def _readout_messages(seen: Dict[str, List]) -> List[dict]:
    return [m for m in seen["inputs"] if m.get("type") == "readout"]


def _open_view(authed_page: Page, base_url: str, *, pin_clock: bool = False) -> Tuple[
    Dict[str, List], List[WebSocketRoute], Page
]:
    """The live view in View mode; ``(seen, sockets, page)``."""
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_frame())

    if pin_clock:
        authed_page.context.add_init_script(_PIN_CLOCK)
    seen = _stub_screen(authed_page.context, on_connect)
    page = _open_from_sheet(authed_page, base_url)
    expect(page.locator("#screenImage")).to_be_visible()
    wait_until(page, lambda: len(seen["acks"]) >= 1, "the first frame's ack")
    return seen, sockets, page


def _open_sheet(page: Page) -> None:
    page.locator("#screenQuality").click()
    expect(page.locator("#screenQualitySheet")).to_be_visible()


def _close_sheet(page: Page) -> None:
    page.locator("#screenQualityDone").click()
    expect(page.locator("#screenQualitySheet")).to_be_hidden()


def _pick(page: Page, preset: str) -> None:
    btn = page.locator(f'#screenPreset .segmented-item[data-preset="{preset}"]')
    btn.click()
    expect(btn).to_have_attribute("aria-pressed", "true")


def test_a_preset_rides_the_connect_query_is_remembered_and_survives_a_reconnect(
    authed_page: Page, base_url: str
) -> None:
    seen, sockets, page = _open_view(authed_page, base_url)
    # Nothing picked yet: Balanced, today's behaviour.
    assert _preset_of(seen["urls"][0]) == "balanced"
    _open_sheet(page)
    labels = page.locator("#screenPreset .segmented-item")
    expect(labels).to_have_text(["Sharp", "Balanced", "Data saver"])
    expect(page.locator('#screenPreset [data-preset="balanced"]')).to_have_attribute("aria-pressed", "true")
    surface(page, "screen.quality-sheet")

    # A pick reconnects with the new preset, and keeps the monitor and width.
    _pick(page, "saver")
    wait_until(page, lambda: len(seen["urls"]) == 2, "the reconnect for the pick")
    query = parse_qs(urlparse(seen["urls"][1]).query)
    assert query["preset"] == ["saver"]
    assert query["monitor"] == parse_qs(urlparse(seen["urls"][0]).query)["monitor"]
    assert query["width"] == parse_qs(urlparse(seen["urls"][0]).query)["width"]
    assert page.evaluate("localStorage.getItem('launcher.screenPreset')") == "saver"
    expect(page.locator("#screenPresetNote")).to_contain_text("960 px")
    # The same pick again is not a reconnect.
    _pick(page, "saver")
    page.wait_for_timeout(300)
    assert len(seen["urls"]) == 2
    _close_sheet(page)
    wait_until(page, lambda: len(seen["acks"]) >= 2, "the new connection's first frame")

    # A dropped socket comes back on its own with the preset still on it.
    sockets[-1].close()
    wait_until(page, lambda: len(seen["urls"]) == 3, "the automatic reconnect", timeout_ms=15_000)
    assert _preset_of(seen["urls"][2]) == "saver"

    # Remembered per device: the next open starts on it, and the sheet shows it.
    page.locator("#screenViewClose").click()
    seen["urls"].clear()
    page2 = _open_from_sheet(authed_page, base_url)
    wait_until(page2, lambda: len(seen["urls"]) >= 1, "the next open's socket")
    assert _preset_of(seen["urls"][0]) == "saver"
    _open_sheet(page2)
    expect(page2.locator('#screenPreset [data-preset="saver"]')).to_have_attribute("aria-pressed", "true")
    _pick(page2, "sharp")
    wait_until(page2, lambda: len(seen["urls"]) == 2, "the pick back to sharp")
    assert _preset_of(seen["urls"][1]) == "sharp"


def test_a_stored_value_that_is_not_a_preset_opens_on_balanced(
    authed_page: Page, base_url: str
) -> None:
    authed_page.context.add_init_script("localStorage.setItem('launcher.screenPreset', 'ludicrous')")
    seen, _sockets, page = _open_view(authed_page, base_url)
    assert _preset_of(seen["urls"][0]) == "balanced"
    _open_sheet(page)
    expect(page.locator('#screenPreset [data-preset="balanced"]')).to_have_attribute("aria-pressed", "true")


def test_the_readout_is_off_until_switched_on_and_off_again_sends_and_shows_nothing(
    authed_page: Page, base_url: str
) -> None:
    seen, sockets, page = _open_view(authed_page, base_url)
    readout = page.locator("#screenReadout")
    toggle = page.locator("#screenReadoutToggle")

    # Opened and streaming: no readout, nothing about one sent.
    expect(readout).to_be_hidden()
    assert _readout_messages(seen) == []
    _open_sheet(page)
    expect(toggle).to_have_attribute("aria-checked", "false")

    # On, from View mode: one small message, the overlay up, no input.
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    wait_until(page, lambda: _readout_messages(seen) == [_READOUT_ON], "the switch-on message")
    _close_sheet(page)
    expect(readout).to_be_visible()
    expect(readout).to_contain_text("fps")
    expect(readout).to_contain_text("PC times: waiting")
    assert [m for m in seen["inputs"] if m.get("type") != "readout"] == []

    # A reconnect is a new socket with the readout off on the server: the page
    # says again that it is on.
    sockets[-1].close()
    wait_until(page, lambda: len(seen["urls"]) == 2, "the automatic reconnect", timeout_ms=15_000)
    wait_until(page, lambda: _readout_messages(seen) == [_READOUT_ON, _READOUT_ON], "the switch-on, again")

    # Off: told, hidden, and a late stats message shows nothing.
    _open_sheet(page)
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    wait_until(
        page, lambda: _readout_messages(seen) == [_READOUT_ON, _READOUT_ON, _READOUT_OFF],
        "the switch-off message",
    )
    _close_sheet(page)
    sockets[-1].send(json.dumps({"type": "stats", "grab": 1, "digest": 1, "resize": 1, "encode": 1, "frames": 1}))
    page.wait_for_timeout(700)
    expect(readout).to_be_hidden()

    # The readout does not outlive the view: closed with it, off on the next open.
    _open_sheet(page)
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    _close_sheet(page)
    page.locator("#screenViewClose").click()
    seen["inputs"].clear()
    page2 = _open_from_sheet(authed_page, base_url)
    expect(page2.locator("#screenImage")).to_be_visible()
    expect(page2.locator("#screenReadout")).to_be_hidden()
    assert _readout_messages(seen) == []


def test_the_readout_numbers_are_the_bytes_and_frames_the_page_received(
    authed_page: Page, base_url: str
) -> None:
    seen, sockets, page = _open_view(authed_page, base_url, pin_clock=True)
    frame = _big_frame()
    size = len(frame)
    assert size > 2000, "the frame must be big enough to read in kB"

    # Switch the readout on at a pinned t = 10 000 ms.
    page.evaluate("window.__t = 10000")
    _open_sheet(page)
    page.locator("#screenReadoutToggle").click()
    wait_until(page, lambda: _readout_messages(seen) == [_READOUT_ON], "the switch-on message")
    _close_sheet(page)

    # Four frames, one every 200 ms. Each is drawn and acked at its own pinned
    # instant before the clock moves on, so every ack-to-next-frame gap is 200 ms.
    acks_before = len(seen["acks"])
    for i in range(1, 5):
        page.evaluate(f"window.__t = {10000 + 200 * i}")
        sockets[-1].send(frame)
        wait_until(page, lambda i=i: len(seen["acks"]) >= acks_before + i, f"frame {i}'s ack")

    # One second after switch-on: 4 frames and 4 * size bytes in 1 s.
    page.evaluate("window.__t = 11000")
    readout = page.locator("#screenReadout")
    expect(readout).to_contain_text("4.0 fps")
    first, second = [line.strip() for line in readout.inner_text().splitlines()[:2]]
    kbps = float(re.fullmatch(r"4\.0 fps · (\d+) kB/s", first).group(1))
    assert abs(kbps - 4 * size / 1000) <= 0.5, (first, size)
    avg = float(re.fullmatch(r"avg (\d+\.\d) kB · ack to frame 200 ms", second).group(1))
    assert abs(avg - size / 1000) <= 0.05, (second, size)

    # The server's stage times fill the last two lines as sent.
    sockets[-1].send(json.dumps({
        "type": "stats", "grab": 12.3, "digest": 3.1, "resize": 9.5, "encode": None, "frames": 4,
    }))
    expect(readout).to_contain_text("PC grab 12.3 · digest 3.1 ms")
    expect(readout).to_contain_text("PC resize 9.5 · encode – ms")
    surface(page, "screen.readout")

    # The overlay is a read-out, never a target.
    expect(readout).to_have_css("pointer-events", "none")


def _reveal_tools(page: Page) -> None:
    """In landscape on a phone the bars sit behind the handle."""
    handle = page.locator("#screenHandle")
    if handle.is_visible() and handle.get_attribute("aria-expanded") == "false":
        handle.click()
    expect(page.locator("#screenQuality")).to_be_visible()


def _box(page: Page, selector: str) -> Dict[str, float]:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return box


# How far the element answers a touch, not how big it is drawn: the fleet's
# segmented control and modal close reach the 44px floor with a hit band beyond
# their box (design.md "Targets"), so the box alone under-reads them.
_HIT_EXTENT_JS = """el => {
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const owns = (x, y) => { const t = document.elementFromPoint(x, y); return !!t && el.contains(t); };
  let up = 0, down = 0, left = 0, right = 0;
  while (up < 40 && owns(cx, cy - r.height / 2 - up - 0.5)) up++;
  while (down < 40 && owns(cx, cy + r.height / 2 + down + 0.5)) down++;
  while (left < 40 && owns(cx - r.width / 2 - left - 0.5, cy)) left++;
  while (right < 40 && owns(cx + r.width / 2 + right + 0.5, cy)) right++;
  return { w: r.width + left + right, h: r.height + up + down };
}"""


def _hit_extent(page: Page, selector: str, nth: int = 0) -> Dict[str, float]:
    return page.locator(selector).nth(nth).evaluate(_HIT_EXTENT_JS)


_LEGS = [
    ("portrait", {"width": 360, "height": 640}),
    ("landscape", {"width": 844, "height": 390}),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("leg", _LEGS, ids=[name for name, _ in _LEGS])
def test_the_button_sheet_and_overlay_fit_a_narrow_phone_with_44px_targets(
    authed_page: Page, base_url: str, leg, theme: str
) -> None:
    _name, viewport = leg
    seen, sockets, page = _open_view(authed_page, base_url)
    page.set_viewport_size(viewport)
    page.emulate_media(color_scheme=theme)
    page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
    vw, vh = viewport["width"], viewport["height"]

    # The widest the tools row gets: Control, with the style switch and the
    # keyboard button beside the new one.
    _reveal_tools(page)
    _set_mode(page, "control")
    expect(page.locator("#screenControl")).to_be_visible()
    expect(page.locator("#screenKeyboard")).to_be_visible()
    quality = _box(page, "#screenQuality")
    keyboard = _box(page, "#screenKeyboard")
    hit = _hit_extent(page, "#screenQuality")
    assert hit["w"] >= 44 and hit["h"] >= 44, hit
    assert quality["x"] >= 0 and quality["x"] + quality["width"] <= vw, quality
    assert quality["x"] >= keyboard["x"] + keyboard["width"] - 1, "the two buttons overlap"
    assert page.locator(".screen-view-tools").evaluate("el => el.scrollWidth <= el.clientWidth")
    _set_mode(page, "view")
    # View: the keyboard button is gone and Quality still has its place.
    expect(page.locator("#screenKeyboard")).to_be_hidden()
    view_quality = _box(page, "#screenQuality")
    assert view_quality["x"] + view_quality["width"] <= vw, view_quality
    hit = _hit_extent(page, "#screenQuality")
    assert hit["w"] >= 44 and hit["h"] >= 44, hit

    # The sheet: every target is 44px, and it fits the window.
    _open_sheet(page)
    for selector in ("#screenQualityDone", "#screenPreset .segmented-item"):
        for i in range(page.locator(selector).count()):
            hit = _hit_extent(page, selector, i)
            assert hit["h"] >= 44 and hit["w"] >= 44, (selector, hit)
    toggle = _hit_extent(page, "#screenReadoutToggle")
    assert toggle["h"] >= 44 and toggle["w"] >= 44, toggle
    card = _box(page, "#screenQualitySheet .detail-card")
    assert card["x"] >= 0 and card["x"] + card["width"] <= vw, card
    sheet_scrolls = page.locator("#screenQualitySheet").evaluate("el => el.scrollHeight > el.clientHeight")
    if not sheet_scrolls:
        assert card["y"] >= 0 and card["y"] + card["height"] <= vh, (card, vh)
    page.locator("#screenQualityDone").scroll_into_view_if_needed()
    done = _box(page, "#screenQualityDone")
    assert 0 <= done["y"] and done["y"] + done["height"] <= vh, (done, vh)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # Every preset name is whole on its button.
    assert page.locator("#screenPreset").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.locator("#screenReadoutToggle").click()
    _close_sheet(page)

    # The overlay: inside the picture's stage, whole, and not wider than it.
    readout = page.locator("#screenReadout")
    sockets[-1].send(json.dumps({"type": "stats", "grab": 123.4, "digest": 12.3, "resize": 45.6, "encode": 78.9, "frames": 7}))
    expect(readout).to_contain_text("encode 78.9")
    box = _box(page, "#screenReadout")
    stage = _box(page, "#screenStage")
    assert stage["x"] <= box["x"] and box["x"] + box["width"] <= stage["x"] + stage["width"], (box, stage)
    assert stage["y"] <= box["y"] and box["y"] + box["height"] <= stage["y"] + stage["height"], (box, stage)
    assert readout.evaluate("el => el.scrollWidth <= el.clientWidth")
