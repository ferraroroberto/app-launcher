"""Regression pin for #1520: the Screen view, against a stub frame source.

The PC load sheet's Screen row opens the view: full screen in the page on a
phone (WebKit/iPhone), its own pop-out window on a desktop browser
(Chromium, fine pointer). Both monitors list as a picker, the view draws the
stub's frame and acks it, a pick reconnects to the other display, two-finger
pinch zooms locally, and the x closes it (the pop-out closes its window).

Nothing here touches the real screen: ``/api/screen/monitors`` and the
``/api/screen/ws`` socket are context-routed (so the pop-out page gets them
too) before navigation, and the frame is a tiny invented JPEG.
"""
from __future__ import annotations

import io
import json
import re
from typing import Dict, List

import pytest
from PIL import Image
from playwright.sync_api import BrowserContext, Page, WebSocketRoute, expect

from tests.e2e.conftest import wait_until

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_MONITORS = {
    "monitors": [
        {"id": 0, "left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},
        {"id": 1, "left": 0, "top": -1080, "width": 1920, "height": 1080, "primary": False},
    ]
}
_FRAME_SIZE = (160, 90)


def _frame() -> bytes:
    img = Image.new("RGB", _FRAME_SIZE, (40, 90, 160))
    img.paste((240, 240, 240), (10, 10, 70, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _stub_screen(context: BrowserContext, on_connect=None) -> Dict[str, List]:
    seen: Dict[str, List] = {"urls": [], "acks": []}
    context.route(
        re.compile(r".*/api/screen/monitors$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MONITORS)
        ),
    )

    def handler(ws: WebSocketRoute) -> None:
        seen["urls"].append(ws.url)
        ws.on_message(lambda msg: seen["acks"].append(msg) if msg == "ack" else None)
        if on_connect:
            on_connect(ws)
        else:
            ws.send(_frame())

    context.route_web_socket(re.compile(r".*/api/screen/ws.*"), handler)
    return seen


def _open_from_sheet(page: Page, base_url: str) -> Page:
    """Tap the CPU reading, then the sheet's Screen row; the page the view is in."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#paneClaude > .home-head .cpu-reading").click()
    expect(page.locator("#cpuSheet")).to_be_visible()
    row = page.locator("#cpuSheetScreen")
    expect(row).to_contain_text("Screen")
    if page.evaluate("matchMedia('(pointer: fine)').matches"):
        with page.context.expect_page() as popup_info:
            row.click()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded")
        expect(page.locator("#screenView")).to_be_hidden()
        return popup
    row.click()
    return page


def test_the_sheet_opens_a_live_view_of_the_chosen_display(
    authed_page: Page, base_url: str
) -> None:
    seen = _stub_screen(authed_page.context)
    view_page = _open_from_sheet(authed_page, base_url)
    popped_out = view_page is not authed_page

    dialog = view_page.locator("#screenView")
    expect(dialog).to_be_visible()
    expect(view_page.locator("#cpuSheet")).to_be_hidden()
    image = view_page.locator("#screenImage")
    expect(image).to_be_visible()
    wait_until(view_page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    assert view_page.evaluate(
        "document.getElementById('screenImage').naturalWidth"
    ) == _FRAME_SIZE[0]
    assert "monitor=0" in seen["urls"][0], "no pick yet: the primary"
    expect(view_page.locator("#screenState")).to_be_hidden()

    # The view fills the window, and nothing (the nav included) sits over it:
    # whatever is at the nav's spot belongs to the view.
    geo = view_page.locator("#screenView").evaluate("""(el) => {
      const r = el.getBoundingClientRect();
      const close = document.getElementById('screenViewClose').getBoundingClientRect();
      const nav = document.querySelector('nav.tabs').getBoundingClientRect();
      const x = Math.min(innerWidth - 1, Math.max(0, nav.left + nav.width / 2));
      const y = Math.min(innerHeight - 1, Math.max(0, nav.top + nav.height / 2));
      const hit = document.elementFromPoint(x, y);
      return { w: r.width, h: r.height, vw: innerWidth, vh: innerHeight,
               closeW: close.width, closeH: close.height,
               navSpotInView: !!hit && el.contains(hit) };
    }""")
    assert abs(geo["w"] - geo["vw"]) <= 1 and abs(geo["h"] - geo["vh"]) <= 1, geo
    assert geo["closeW"] >= 34 and geo["closeH"] >= 34, geo
    assert geo["navSpotInView"], "the nav must not sit over the view"

    # Both displays as a picker; the one above the primary reconnects.
    picker = view_page.locator("#screenPicker .range-tab")
    expect(picker).to_have_text(["Main", "Above"])
    expect(picker.nth(0)).to_have_attribute("aria-pressed", "true")
    picker.nth(1).click()
    wait_until(view_page, lambda: len(seen["urls"]) == 2, "the reconnect")
    assert "monitor=1" in seen["urls"][1]
    expect(picker.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(image).to_be_visible()

    # Two-finger pinch zooms the frame locally.
    view_page.evaluate("""() => {
      const s = document.getElementById('screenStage');
      const r = s.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const ev = (type, id, x) => s.dispatchEvent(new PointerEvent(type, {
        pointerId: id, clientX: x, clientY: cy, bubbles: true, pointerType: 'touch' }));
      ev('pointerdown', 1, cx - 20); ev('pointerdown', 2, cx + 20);
      ev('pointermove', 1, cx - 60); ev('pointermove', 2, cx + 60);
      ev('pointerup', 1, cx - 60); ev('pointerup', 2, cx + 60);
    }""")
    expect(image).to_have_css("transform", re.compile(r"matrix\(3, 0, 0, 3,"))

    if popped_out:
        with view_page.expect_event("close"):
            view_page.locator("#screenViewClose").click()
    else:
        view_page.locator("#screenViewClose").click()
        expect(dialog).to_be_hidden()


def test_a_locked_screen_and_a_refusal_each_say_so(
    authed_page: Page, base_url: str
) -> None:
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(json.dumps({"type": "status", "state": "unavailable"}))

    seen = _stub_screen(authed_page.context, on_connect)
    view_page = _open_from_sheet(authed_page, base_url)

    state = view_page.locator("#screenState")
    expect(state).to_contain_text("locked or asleep")
    expect(view_page.locator("#screenImage")).to_be_hidden()

    # The gate's refusal, closed from here: a sync close inside the route
    # handler would wait on the dispatcher it runs on.
    sockets[-1].close(code=4403, reason="screen is Tailscale-only")
    expect(state).to_contain_text("Tailscale-only")
    retry = state.locator(".async-retry")
    expect(retry).to_be_visible()
    retry.click()
    wait_until(view_page, lambda: len(seen["urls"]) == 2, "the retry's reconnect")
    expect(state).to_contain_text("locked or asleep")
