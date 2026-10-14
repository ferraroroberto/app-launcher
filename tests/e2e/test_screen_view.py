"""Regression pin for #1520: the Screen view, against a stub frame source.

The PC load sheet's Screen row opens the view: full screen in the page on a
phone (WebKit/iPhone), its own pop-out window on a desktop browser
(Chromium, fine pointer). Both monitors list as a picker, the view draws the
stub's frame and acks it, a pick reconnects to the other display, two-finger
pinch zooms locally, and the x closes it (the pop-out closes its window).

Nothing here touches the real screen: ``/api/screen/monitors`` and the
``/api/screen/ws`` socket are context-routed (so the pop-out page gets them
too) before navigation, and the frame is a tiny invented JPEG.

Control (#1521): in Control mode a tap, double-tap, long-press and drag send
the input messages the stub records, with the tapped spot as 0-1 in the
frame; View mode sends nothing; the keyboard types text and sticky-modifier
combos. No input reaches a PC: the stub socket is the only receiver.

Trackpad (#1599): the finger moves a cursor by how far it travels, taps
click at the cursor, and the Control style is remembered per device.
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
    """Route the monitor list and the socket; ``seen`` collects the socket
    URLs, the frame acks, and every other message the view sends (the
    input messages, #1521), parsed."""
    seen: Dict[str, List] = {"urls": [], "acks": [], "inputs": []}

    def on_message(msg) -> None:
        if msg == "ack":
            seen["acks"].append(msg)
        else:
            seen["inputs"].append(json.loads(msg))
    context.route(
        re.compile(r".*/api/screen/monitors$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MONITORS)
        ),
    )

    def handler(ws: WebSocketRoute) -> None:
        seen["urls"].append(ws.url)
        ws.on_message(on_message)
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
    picker = view_page.locator("#screenPicker .segmented-item")
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


# ------------------------------------------------------------ control (#1521)
#
# Taps are synthetic pointer events dispatched at a point on the drawn
# picture (the swipe rows' approach, #1503): Playwright's touch emulation
# does not reach a WebKit pointer stream on every host. The point is placed
# from the rendered stage and the image's natural size, independently of
# the app's own mapping, and the socket stub records what the view sends.

_POINT_JS = """([fx, fy]) => {
  const s = document.getElementById('screenStage');
  const img = document.getElementById('screenImage');
  const r = s.getBoundingClientRect();
  const fit = Math.min(s.clientWidth / img.naturalWidth, s.clientHeight / img.naturalHeight);
  const w = img.naturalWidth * fit, h = img.naturalHeight * fit;
  return { x: r.left + (s.clientWidth - w) / 2 + fx * w,
           y: r.top + (s.clientHeight - h) / 2 + fy * h };
}"""

_POINTER_JS = """([type, x, y, id]) => {
  document.getElementById('screenStage').dispatchEvent(new PointerEvent(type, {
    pointerId: id, clientX: x, clientY: y, bubbles: true, pointerType: 'touch',
    isPrimary: id === 1, button: 0 }));
}"""


def _point(page: Page, fx: float, fy: float) -> Dict[str, float]:
    return page.evaluate(_POINT_JS, [fx, fy])


def _pointer(page: Page, kind: str, pt: Dict[str, float], pointer_id: int = 1) -> None:
    page.evaluate(_POINTER_JS, [kind, pt["x"], pt["y"], pointer_id])


def _tap(page: Page, fx: float, fy: float) -> None:
    pt = _point(page, fx, fy)
    _pointer(page, "pointerdown", pt)
    _pointer(page, "pointerup", pt)


def _live_view(authed_page: Page, base_url: str):
    seen = _stub_screen(authed_page.context)
    view_page = _open_from_sheet(authed_page, base_url)
    expect(view_page.locator("#screenImage")).to_be_visible()
    wait_until(view_page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    return seen, view_page


def _set_mode(page: Page, mode: str) -> None:
    btn = page.locator(f'#screenMode .segmented-item[data-mode="{mode}"]')
    btn.click()
    expect(btn).to_have_attribute("aria-pressed", "true")


def _near(a: float, b: float) -> bool:
    return abs(a - b) < 0.01


def test_control_mode_taps_send_where_tapped_and_view_mode_sends_nothing(
    authed_page: Page, base_url: str
) -> None:
    seen, page = _live_view(authed_page, base_url)

    # View is the default: a tap only touches the local view, the keyboard
    # toggle is not offered, and nothing goes to the PC.
    expect(page.locator('#screenMode .segmented-item[data-mode="view"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator("#screenKeyboard")).to_be_hidden()
    _tap(page, 0.5, 0.5)

    # Control: a tap is a left click at the tapped spot, 0-1 in the frame.
    _set_mode(page, "control")
    expect(page.locator("#screenKeyboard")).to_be_visible()
    _tap(page, 0.25, 0.75)
    wait_until(page, lambda: len(seen["inputs"]) >= 1, "the click")
    click = seen["inputs"][0]
    assert (click["type"], click["button"]) == ("click", "left"), seen["inputs"]
    assert _near(click["x"], 0.25) and _near(click["y"], 0.75), click
    # Socket messages arrive in order, so the View-mode tap sent nothing.
    assert len(seen["inputs"]) == 1, seen["inputs"]

    # A second tap at once, a few px off: the double click's second click
    # lands on the first one's spot.
    near = _point(page, 0.25, 0.75)
    near = {"x": near["x"] + 4, "y": near["y"] + 3}
    _pointer(page, "pointerdown", near)
    _pointer(page, "pointerup", near)
    wait_until(page, lambda: len(seen["inputs"]) >= 2, "the double click")
    assert (seen["inputs"][1]["x"], seen["inputs"][1]["y"]) == (click["x"], click["y"])

    # Long-press: a right click where the finger rests.
    held = _point(page, 0.6, 0.4)
    _pointer(page, "pointerdown", held)
    wait_until(page, lambda: len(seen["inputs"]) >= 3, "the long-press")
    _pointer(page, "pointerup", held)
    right = seen["inputs"][2]
    assert (right["type"], right["button"]) == ("click", "right"), seen["inputs"]
    assert _near(right["x"], 0.6) and _near(right["y"], 0.4), right

    # Press and drag: down at the start, moves, up at the end.
    a, b = _point(page, 0.2, 0.2), _point(page, 0.7, 0.5)
    _pointer(page, "pointerdown", a)
    _pointer(page, "pointermove", {"x": (a["x"] + b["x"]) / 2, "y": (a["y"] + b["y"]) / 2})
    _pointer(page, "pointermove", b)
    _pointer(page, "pointerup", b)
    wait_until(page, lambda: seen["inputs"][-1]["type"] == "up", "the drag's up")
    drag = seen["inputs"][3:]
    assert drag[0]["type"] == "down" and _near(drag[0]["x"], 0.2), drag
    assert all(m["type"] == "move" for m in drag[1:-1]) and len(drag) >= 3, drag
    assert _near(drag[-1]["x"], 0.7) and _near(drag[-1]["y"], 0.5), drag

    # Back to View: taps are local again.
    _set_mode(page, "view")
    expect(page.locator("#screenKeyboard")).to_be_hidden()
    count = len(seen["inputs"])
    _tap(page, 0.5, 0.5)
    _set_mode(page, "control")
    _tap(page, 0.1, 0.1)
    wait_until(page, lambda: len(seen["inputs"]) > count, "the next control click")
    assert len(seen["inputs"]) == count + 1, seen["inputs"][count:]


def test_the_keyboard_types_text_and_sticky_modifier_combos(
    authed_page: Page, base_url: str
) -> None:
    seen, page = _live_view(authed_page, base_url)
    _set_mode(page, "control")
    toggle = page.locator("#screenKeyboard")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    keys = page.locator("#screenKeys")
    expect(keys).to_be_visible()
    expect(page.locator("#screenKeyInput")).to_be_focused()

    # Every key of the first page stays at the 44px floor: the original 13,
    # then the page toggle and Task Mgr (#1602).
    sizes = page.locator("#screenKeys .key-btn:visible").evaluate_all(
        "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; })"
    )
    assert len(sizes) == 15
    assert all(w >= 40 and h >= 44 for w, h in sizes), sizes

    # Typed text goes out verbatim, Unicode included.
    page.keyboard.type("ñ€ hola")
    wait_until(
        page,
        lambda: "".join(m.get("text", "") for m in seen["inputs"]) == "ñ€ hola",
        "the typed text",
    )
    assert {m["type"] for m in seen["inputs"]} == {"text"}

    # Backspace from the phone keyboard is the Backspace key.
    page.keyboard.press("Backspace")
    wait_until(page, lambda: seen["inputs"][-1].get("key") == "backspace", "Backspace")

    # Sticky Ctrl: held for the next key, which goes out as a shortcut, then
    # it lets go.
    ctrl = keys.locator('[data-mod="ctrl"]')
    ctrl.click()
    expect(ctrl).to_have_attribute("aria-pressed", "true")
    page.keyboard.type("a")
    wait_until(page, lambda: seen["inputs"][-1].get("key") == "a", "Ctrl+A")
    assert seen["inputs"][-1] == {"type": "key", "key": "a", "mods": ["ctrl"]}
    expect(ctrl).to_have_attribute("aria-pressed", "false")
    ctrl.click()
    page.keyboard.type("c")
    wait_until(page, lambda: seen["inputs"][-1].get("key") == "c", "Ctrl+C")
    assert seen["inputs"][-1]["mods"] == ["ctrl"]

    # The bar's own keys.
    keys.locator('[data-key="esc"]').click()
    wait_until(page, lambda: seen["inputs"][-1].get("key") == "esc", "Esc")
    assert seen["inputs"][-1] == {"type": "key", "key": "esc", "mods": []}

    # The toggle takes the bar away again.
    toggle.click()
    expect(keys).to_be_hidden()
    expect(toggle).to_have_attribute("aria-pressed", "false")


# ------------------------------------------------- the second page (#1602)

_KEY_BOXES_JS = """els => els.map(e => {
  const r = e.getBoundingClientRect();
  return { w: r.width, h: r.height, l: r.left, r: r.right, t: r.top, b: r.bottom,
           label: e.dataset.key || e.dataset.mod || e.dataset.macro || e.dataset.pageTo };
})"""


def _open_keys(authed_page: Page, base_url: str):
    seen, page = _live_view(authed_page, base_url)
    _set_mode(page, "control")
    page.locator("#screenKeyboard").click()
    expect(page.locator("#screenKeys")).to_be_visible()
    return seen, page


def _last(seen) -> dict:
    return seen["inputs"][-1]


def _visible_boxes(page: Page) -> list:
    return page.locator("#screenKeys .key-btn:visible").evaluate_all(_KEY_BOXES_JS)


def test_the_second_page_sends_f_keys_navigation_keys_and_task_manager(
    authed_page: Page, base_url: str
) -> None:
    seen, page = _open_keys(authed_page, base_url)
    keys = page.locator("#screenKeys")
    main = keys.locator('[data-keys-page="main"]')
    fn = keys.locator('[data-keys-page="fn"]')

    # The first page is the default, and the second is not on screen.
    expect(main).not_to_be_hidden()
    expect(fn).to_be_hidden()
    expect(keys.locator('[data-key="f5"]')).to_be_hidden()

    # Task Mgr is on the first page: one combo, no server change.
    keys.locator('[data-macro="taskmgr"]:visible').click()
    wait_until(page, lambda: seen["inputs"] and _last(seen).get("key") == "esc", "Task Mgr")
    assert _last(seen) == {"type": "key", "key": "esc", "mods": ["ctrl", "shift"]}

    # A modifier tapped on the first page is still held on the second, and
    # the way back names it.
    keys.locator('[data-mod="ctrl"]').click()
    keys.locator('[data-page-to="fn"]').click()
    expect(main).to_be_hidden()
    expect(fn).not_to_be_hidden()
    held = keys.locator("[data-held]")
    expect(held).to_have_text("Ctrl")
    keys.locator('[data-key="f5"]').click()
    wait_until(page, lambda: _last(seen).get("key") == "f5", "Ctrl+F5")
    assert _last(seen) == {"type": "key", "key": "f5", "mods": ["ctrl"]}
    # ...and lets go after that one key, as on the first page.
    expect(held).to_be_hidden()
    keys.locator('[data-key="f12"]').click()
    wait_until(page, lambda: _last(seen).get("key") == "f12", "F12")
    assert _last(seen)["mods"] == []

    # Alt + F4, with two modifiers going back and forth across the pages.
    keys.locator('[data-page-to="main"]').click()
    keys.locator('[data-mod="alt"]').click()
    keys.locator('[data-mod="shift"]').click()
    keys.locator('[data-page-to="fn"]').click()
    expect(held).to_have_text("Alt+Shift")
    keys.locator('[data-key="f4"]').click()
    wait_until(page, lambda: _last(seen).get("key") == "f4", "Alt+Shift+F4")
    assert _last(seen)["mods"] == ["alt", "shift"]

    # The navigation cluster.
    for key in ("home", "end", "pageup", "pagedown"):
        keys.locator(f'[data-key="{key}"]').click()
        wait_until(page, lambda k=key: _last(seen).get("key") == k, key)
        assert _last(seen) == {"type": "key", "key": key, "mods": []}

    # Task Mgr is here too, with the same combo.
    keys.locator('[data-macro="taskmgr"]:visible').click()
    wait_until(page, lambda: _last(seen).get("key") == "esc", "Task Mgr again")
    assert _last(seen) == {"type": "key", "key": "esc", "mods": ["ctrl", "shift"]}

    # Closing the keyboard puts the first page back, for the next time.
    page.locator("#screenKeyboard").click()
    page.locator("#screenKeyboard").click()
    expect(main).not_to_be_hidden()
    expect(fn).to_be_hidden()


def test_a_modifier_and_punctuation_goes_out_as_a_key_combo(
    authed_page: Page, base_url: str
) -> None:
    seen, page = _open_keys(authed_page, base_url)
    keys = page.locator("#screenKeys")

    keys.locator('[data-mod="ctrl"]').click()
    page.keyboard.type("/")
    wait_until(page, lambda: seen["inputs"] and _last(seen).get("key") == "/", "Ctrl+/")
    assert _last(seen) == {"type": "key", "key": "/", "mods": ["ctrl"]}

    # A character that needs Shift on the PC's layout is sent as the
    # character: the server decides where the Shift goes.
    keys.locator('[data-mod="alt"]').click()
    page.keyboard.type("?")
    wait_until(page, lambda: _last(seen).get("key") == "?", "Alt+?")
    assert _last(seen) == {"type": "key", "key": "?", "mods": ["alt"]}

    # Without a modifier, punctuation is still text; so is an accent with one.
    page.keyboard.type(";")
    wait_until(page, lambda: _last(seen).get("text") == ";", "plain ;")
    keys.locator('[data-mod="ctrl"]').click()
    page.keyboard.type("ñ")
    wait_until(page, lambda: _last(seen).get("text") == "ñ", "Ctrl + ñ stays text")
    assert _last(seen)["type"] == "text"


def test_both_pages_fit_a_narrow_phone_in_portrait_and_landscape(
    authed_page: Page, base_url: str
) -> None:
    _seen, page = _open_keys(authed_page, base_url)
    keys = page.locator("#screenKeys")

    for width, height, rows in ((360, 740, 3), (844, 390, 2)):
        page.set_viewport_size({"width": width, "height": height})
        for target, expected in (("fn", 18), ("main", 15)):
            keys.locator(f'[data-page-to="{target}"]').click()
            expect(keys.locator(f'[data-keys-page="{target}"]')).not_to_be_hidden()
            boxes = stable_boxes(page, expected)
            assert len(boxes) == expected, (width, target, boxes)
            assert all(b["h"] >= 44 and b["w"] >= 43.5 for b in boxes), (width, target, boxes)
            assert all(b["l"] >= -0.5 and b["r"] <= width + 0.5 for b in boxes), (width, target, boxes)
            assert len({round(b["t"]) for b in boxes}) == rows, (width, target, rows, boxes)
            assert page.evaluate(
                "() => document.getElementById('screenKeys').scrollWidth"
                " <= document.getElementById('screenKeys').clientWidth + 1"
            ), (width, target)


def stable_boxes(page: Page, expected: int) -> list:
    """The visible keys' boxes once the count settles (a page flip and a
    resize each re-lay the bar out before they read)."""
    boxes: list = []

    def settled() -> bool:
        nonlocal boxes
        boxes = _visible_boxes(page)
        return len(boxes) == expected

    wait_until(page, settled, f"{expected} visible keys")
    return boxes


# -------------------------------------------------------------- cursor (#1598)
#
# The stub socket plays the server's cursor messages. Where the cursor should
# sit is worked out from the rendered picture (its transformed box and the
# frame's natural size), independently of the app's own stagePoint maths.
# The overlay is a 0x0 anchor, which Playwright never calls visible, so its
# shown / hidden state is read from its own ``hidden`` property.

_ZOOMED_POINT_JS = """([fx, fy]) => {
  const img = document.getElementById('screenImage');
  const r = img.getBoundingClientRect();
  const s = r.width / img.clientWidth;
  const fit = Math.min(img.clientWidth / img.naturalWidth, img.clientHeight / img.naturalHeight);
  const w = img.naturalWidth * fit, h = img.naturalHeight * fit;
  return { x: r.left + ((img.clientWidth - w) / 2 + fx * w) * s,
           y: r.top + ((img.clientHeight - h) / 2 + fy * h) * s };
}"""

_CURSOR_JS = """() => {
  const el = document.getElementById('screenCursor');
  const a = el.getBoundingClientRect();
  const svg = el.querySelector('svg');
  const b = svg ? svg.getBoundingClientRect() : { width: 0, height: 0 };
  const hit = document.elementFromPoint(a.left, a.top);
  return { x: a.left, y: a.top, w: b.width, h: b.height, hidden: el.hidden,
           hitsCursor: !!hit && el.contains(hit),
           hitsStage: !!hit && document.getElementById('screenStage').contains(hit) };
}"""


def _send_cursor(ws: WebSocketRoute, **fields) -> None:
    ws.send(json.dumps({"type": "cursor", **fields}))


def _cursor_on(page: Page, fx: float, fy: float, what: str) -> Dict:
    """Wait for the drawn cursor's hotspot to sit on frame point (fx, fy)."""
    state: Dict = {}

    def there() -> bool:
        want = page.evaluate(_ZOOMED_POINT_JS, [fx, fy])
        state.update(page.evaluate(_CURSOR_JS))
        return (not state["hidden"] and abs(state["x"] - want["x"]) <= 1
                and abs(state["y"] - want["y"]) <= 1)

    wait_until(page, there, what)
    return state


def test_the_pc_cursor_follows_its_messages_and_never_takes_a_touch(
    authed_page: Page, base_url: str
) -> None:
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_frame())

    seen = _stub_screen(authed_page.context, on_connect)
    page = _open_from_sheet(authed_page, base_url)
    image = page.locator("#screenImage")
    expect(image).to_be_visible()
    wait_until(page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    cursor = page.locator("#screenCursor")
    expect(cursor).to_have_js_property("hidden", True)  # no cursor message yet
    expect(cursor).to_have_css("pointer-events", "none")

    # A cursor message draws it on its spot, in its shape.
    _send_cursor(sockets[-1], x=0.25, y=0.75, shape="text", visible=True)
    expect(cursor).to_have_attribute("data-shape", "text")
    first = _cursor_on(page, 0.25, 0.75, "the cursor on its spot")
    assert first["w"] == 24 and first["h"] == 24, first
    # It never takes a touch: what is under it is the stage.
    assert first["hitsStage"] and not first["hitsCursor"], first

    # It moves with no new frame, and an unknown shape is the arrow.
    _send_cursor(sockets[-1], x=0.6, y=0.4, shape="some-app-cursor", visible=True)
    expect(cursor).to_have_attribute("data-shape", "arrow")
    _cursor_on(page, 0.6, 0.4, "the moved cursor")
    assert len(seen["acks"]) == 1, "the cursor moved without a frame"

    # Zoomed 3x: still on its spot of the zoomed picture, still 24px.
    page.evaluate("""() => {
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
    zoomed = _cursor_on(page, 0.6, 0.4, "the cursor on the zoomed picture")
    assert zoomed["w"] == 24 and zoomed["h"] == 24, zoomed

    # Panned off the stage by the zoom: not drawn.
    _send_cursor(sockets[-1], x=0.02, y=0.5, shape="arrow", visible=True)
    expect(cursor).to_have_js_property("hidden", True)

    # Back on screen, then the PC hides it.
    _send_cursor(sockets[-1], x=0.5, y=0.5, shape="hand", visible=True)
    expect(cursor).to_have_attribute("data-shape", "hand")
    expect(cursor).to_have_js_property("hidden", False)
    # An unreadable desktop: no cursor over the stale frame, until the
    # picture is live again.
    sockets[-1].send(json.dumps({"type": "status", "state": "unavailable"}))
    expect(cursor).to_have_js_property("hidden", True)
    sockets[-1].send(_frame())
    expect(cursor).to_have_js_property("hidden", False)
    _send_cursor(sockets[-1], visible=False)
    expect(cursor).to_have_js_property("hidden", True)


def test_a_control_tap_on_the_cursor_still_clicks_the_pc(
    authed_page: Page, base_url: str
) -> None:
    """The overlay sits on the tapped spot after a click; a real tap there
    must still reach the stage and click."""
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_frame())

    seen = _stub_screen(authed_page.context, on_connect)
    page = _open_from_sheet(authed_page, base_url)
    expect(page.locator("#screenImage")).to_be_visible()
    wait_until(page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    _set_mode(page, "control")
    _send_cursor(sockets[-1], x=0.5, y=0.5, shape="arrow", visible=True)
    at = _cursor_on(page, 0.5, 0.5, "the cursor in the middle")
    page.mouse.click(at["x"] + 1, at["y"] + 1)
    wait_until(page, lambda: len(seen["inputs"]) >= 1, "the click through the cursor")
    click = seen["inputs"][0]
    assert click["type"] == "click" and _near(click["x"], 0.5) and _near(click["y"], 0.5), click


# ------------------------------------------------------------ trackpad (#1599)
#
# Trackpad moves the cursor by how far the finger travels, wherever it lands,
# never clicks on a move, and clicks at the cursor (the Windows remote desktop
# app's mouse-pointer gestures). Where the cursor is, is read back from the move
# messages the stub records, and the drawn overlay is checked against them.


def _drag(page: Page, start: Dict[str, float], dx: float, steps: int = 3) -> None:
    _pointer(page, "pointerdown", start)
    for i in range(1, steps + 1):
        _pointer(page, "pointermove", {"x": start["x"] + dx * i / steps, "y": start["y"]})
    _pointer(page, "pointerup", {"x": start["x"] + dx, "y": start["y"]})


_SEQUENCE_JS = """(steps) => {
  const s = document.getElementById('screenStage');
  for (const [type, x, y, id] of steps) {
    s.dispatchEvent(new PointerEvent(type, {
      pointerId: id, clientX: x, clientY: y, bubbles: true, pointerType: 'touch',
      isPrimary: id === 1, button: 0 }));
  }
}"""


def _sequence(page: Page, steps: List) -> None:
    """Several pointer events in one evaluate, so a double-tap stays inside
    its wait however slow the round trips are."""
    page.evaluate(_SEQUENCE_JS, [[t, pt["x"], pt["y"], i] for t, pt, i in steps])


def _settled(page: Page, seen: Dict[str, List], what: str) -> None:
    """Moves coalesce per animation frame: wait until the newest one the PC
    got is where the cursor is drawn (it is drawn at once, so the two agree
    only once the drag's last move is in)."""
    def there() -> bool:
        moves = [m for m in seen["inputs"] if m["type"] == "move"]
        if not moves:
            return False
        want = page.evaluate(_ZOOMED_POINT_JS, [moves[-1]["x"], moves[-1]["y"]])
        got = page.evaluate(_CURSOR_JS)
        return (not got["hidden"] and abs(got["x"] - want["x"]) <= 1
                and abs(got["y"] - want["y"]) <= 1)

    wait_until(page, there, what)


def _held(page: Page) -> None:
    wait_until(
        page,
        lambda: page.evaluate("document.getElementById('screenCursor').classList.contains('held')"),
        "the long-press to hold",
    )


def test_trackpad_moves_a_cursor_clicks_at_it_and_is_remembered(
    authed_page: Page, base_url: str
) -> None:
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_frame())

    seen = _stub_screen(authed_page.context, on_connect)
    page = _open_from_sheet(authed_page, base_url)
    expect(page.locator("#screenImage")).to_be_visible()
    wait_until(page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    style = page.locator("#screenControl")
    touch = style.locator('[data-control="touch"]')
    trackpad = style.locator('[data-control="trackpad"]')

    # The style switch is Control's: hidden in View, and a first use on this
    # device is direct touch.
    expect(style).to_be_hidden()
    _set_mode(page, "control")
    expect(style).to_be_visible()
    expect(touch).to_have_attribute("aria-pressed", "true")
    # Both switches and the keyboard button share the row, every label whole.
    fit = page.locator(".screen-view-tools").evaluate("""(row) => ({
      row: row.scrollWidth <= row.clientWidth,
      labels: Array.from(row.querySelectorAll('.segmented-item')).every(
        (b) => b.scrollWidth <= b.clientWidth),
    })""")
    assert fit == {"row": True, "labels": True}, fit

    # Trackpad: the cursor shows mid-picture before the PC has said where its
    # own is, and the keyboard is still offered.
    trackpad.click()
    expect(trackpad).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#screenKeyboard")).to_be_visible()
    _cursor_on(page, 0.5, 0.5, "the trackpad cursor mid-picture")

    # A drag that starts far off, low on the left, moves the cursor right
    # from where it was, not to the finger, and clicks nothing.
    _drag(page, _point(page, 0.1, 0.9), 40, steps=4)
    _settled(page, seen, "the cursor where it moved")
    moves = [m for m in seen["inputs"] if m["type"] == "move"]
    assert all(m["y"] == 0.5 for m in moves), moves
    assert 0.5 < moves[0]["x"] <= moves[-1]["x"] < 0.8, moves
    assert all(m["type"] == "move" for m in seen["inputs"]), "a move clicks nothing"
    x0 = moves[-1]["x"]

    # A finger that rests (long enough to hold) and then moves still only
    # moves the cursor: no button goes down, nothing clicks.
    count = len(seen["inputs"])
    start = _point(page, 0.6, 0.6)
    _pointer(page, "pointerdown", start)
    _held(page)
    for i in (1, 2, 3, 4):
        _pointer(page, "pointermove", {"x": start["x"] + 10 * i, "y": start["y"]})
    _pointer(page, "pointerup", {"x": start["x"] + 40, "y": start["y"]})
    _settled(page, seen, "the cursor after a rest, then a move")
    rested = seen["inputs"][count:]
    assert rested and all(m["type"] == "move" for m in rested), rested
    x1 = rested[-1]["x"]
    assert x0 < x1 < 0.95, (x0, x1)

    # A tap anywhere clicks at the cursor, once its wait for a second touch
    # is over.
    count = len(seen["inputs"])
    _tap(page, 0.9, 0.1)
    wait_until(page, lambda: len(seen["inputs"]) > count, "the tap's click")
    assert seen["inputs"][count:] == [{"type": "click", "button": "left", "x": x1, "y": 0.5}]

    # Two quick taps, anywhere: a double click on the cursor's spot.
    count = len(seen["inputs"])
    a, b = _point(page, 0.2, 0.3), _point(page, 0.7, 0.8)
    _sequence(page, [("pointerdown", a, 1), ("pointerup", a, 1),
                     ("pointerdown", b, 1), ("pointerup", b, 1)])
    wait_until(page, lambda: len(seen["inputs"]) >= count + 2, "the double click")
    left = {"type": "click", "button": "left", "x": x1, "y": 0.5}
    assert seen["inputs"][count:] == [left, left], seen["inputs"][count:]

    # A long-press lifted where it rests right-clicks at the cursor.
    count = len(seen["inputs"])
    rest = _point(page, 0.3, 0.3)
    _pointer(page, "pointerdown", rest)
    _held(page)
    _pointer(page, "pointerup", rest)
    wait_until(page, lambda: len(seen["inputs"]) > count, "the right click")
    right = {"type": "click", "button": "right", "x": x1, "y": 0.5}
    assert seen["inputs"][count:] == [right], seen["inputs"][count:]

    # So does a two-finger tap.
    count = len(seen["inputs"])
    p, q = _point(page, 0.4, 0.5), _point(page, 0.6, 0.5)
    _sequence(page, [("pointerdown", p, 1), ("pointerdown", q, 2),
                     ("pointerup", p, 1), ("pointerup", q, 2)])
    wait_until(page, lambda: len(seen["inputs"]) > count, "the two-finger right click")
    assert seen["inputs"][count:] == [right], seen["inputs"][count:]

    # Double-tap and hold, then drag: the button goes down at the cursor,
    # with no click first, and comes up where the drag ends.
    count = len(seen["inputs"])
    s = _point(page, 0.4, 0.4)
    _sequence(page, [("pointerdown", s, 1), ("pointerup", s, 1), ("pointerdown", s, 1)]
              + [("pointermove", {"x": s["x"] + 10 * i, "y": s["y"]}, 1) for i in (1, 2, 3, 4)]
              + [("pointerup", {"x": s["x"] + 40, "y": s["y"]}, 1)])
    wait_until(page, lambda: seen["inputs"][-1]["type"] == "up", "the drag's up")
    drag = seen["inputs"][count:]
    assert drag[0] == {"type": "down", "button": "left", "x": x1, "y": 0.5}, drag
    assert all(m["type"] == "move" for m in drag[1:-1]), drag
    assert drag[-1]["type"] == "up" and drag[-1]["x"] > x1 and drag[-1]["y"] == 0.5, drag

    # The PC's own cursor moved: the next drag starts from there.
    _send_cursor(sockets[-1], x=0.25, y=0.25, shape="arrow", visible=True)
    _cursor_on(page, 0.25, 0.25, "the trackpad cursor taking the PC's")
    count = len(seen["inputs"])
    _drag(page, _point(page, 0.8, 0.8), 40, steps=4)
    wait_until(page, lambda: len(seen["inputs"]) > count, "the moves from the PC's cursor")
    moved = [m for m in seen["inputs"][count:] if m["type"] == "move"]
    assert moved and 0.25 < moved[-1]["x"] < 0.6 and moved[-1]["y"] == 0.25, moved

    # Remembered for this device: back from View, and across a reload, the
    # next Control is Trackpad again. The view itself still opens in View.
    _set_mode(page, "view")
    expect(style).to_be_hidden()
    _set_mode(page, "control")
    expect(trackpad).to_have_attribute("aria-pressed", "true")
    if page is not authed_page:
        with page.expect_event("close"):
            page.locator("#screenViewClose").click()
    page = _open_from_sheet(authed_page, base_url)
    expect(page.locator('#screenMode .segmented-item[data-mode="view"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator("#screenControl")).to_be_hidden()
    _set_mode(page, "control")
    expect(page.locator('#screenControl [data-control="trackpad"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    page.locator('#screenControl [data-control="touch"]').click()
    assert page.evaluate("localStorage.getItem('launcher.screenControlMode')") == "touch"
