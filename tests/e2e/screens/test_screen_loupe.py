"""Regression pin for #1725: the magnifier loupe in Touch control is only a picture.

One finger down in Touch control shows a round loupe above it, 150 ms later,
with the frame under the finger at 2.5x; it follows the finger while it drags
and is gone on lift. It is purely visual: ``pointer-events: none``, and the
click, drag and long-press points the PC gets are the ones it got without it
(``test_control_mode_taps_send_where_tapped_and_view_mode_sends_nothing`` still
pins those unchanged). It is hidden in View, in Trackpad, with two fingers down
and while the picture is not live. The geometry (flip under the finger near the
top, clamp at the sides) is pinned in plain Node by
``tests/js/screen_gestures.test.mjs``; here the same edges are read off the
rendered stage.

Disposable webapp and the stub socket only. The 150 ms and 500 ms are the
page's own timers on a paused fake clock (``context.clock``), so "not yet at
149 ms, there at 150 ms, gone at the long-press" is exact instead of racing the
host's load; the drag test runs on the real clock because it holds the finger
down for good.
"""
from __future__ import annotations

import json
from typing import Dict, List

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e.fixtures.waits import wait_until
from tests.e2e.screens.test_screen_view import (
    _frame,
    _near,
    _open_from_sheet,
    _point,
    _pointer,
    _set_mode,
    _stub_screen,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_LOUPE_JS = """() => {
  const l = document.getElementById('screenLoupe');
  const s = document.getElementById('screenStage');
  const lr = l.getBoundingClientRect(), sr = s.getBoundingClientRect();
  const at = document.elementFromPoint(lr.left + lr.width / 2, lr.top + lr.height / 2);
  const cs = getComputedStyle(l);
  const img = document.getElementById("screenImage");
  const drawnW = img.naturalWidth * Math.min(sr.width / img.naturalWidth, sr.height / img.naturalHeight);
  return { hidden: l.hidden, x: lr.left - sr.left, y: lr.top - sr.top, w: lr.width, h: lr.height,
           stageW: sr.width, stageH: sr.height, side: l.dataset.side,
           hitsLoupe: !!at && l.contains(at), hitsStage: at === s,
           pointerEvents: cs.pointerEvents, bgSize: cs.backgroundSize, ariaHidden: l.getAttribute("aria-hidden"), drawnW: drawnW };
}"""

_STAGE_JS = """() => {
  const r = document.getElementById('screenStage').getBoundingClientRect();
  return { left: r.left, top: r.top, w: r.width, h: r.height };
}"""

_GAP = 44  # LOUPE.gap in screen-gestures.js: clear of the fingertip


def _loupe(page: Page) -> Dict:
    return page.evaluate(_LOUPE_JS)


def _on_stage(page: Page, x: float, y: float) -> Dict[str, float]:
    """The client point at stage-local (x, y)."""
    s = page.evaluate(_STAGE_JS)
    return {"x": s["left"] + x, "y": s["top"] + y}


def _live_touch_view(authed_page: Page, base_url: str, paused: bool):
    """A live view in Touch control; with ``paused``, on a paused fake clock."""
    sockets: List[WebSocketRoute] = []

    def on_connect(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_frame())

    context = authed_page.context
    if paused:
        context.clock.install()
    seen = _stub_screen(context, on_connect)
    page = _open_from_sheet(authed_page, base_url)
    expect(page.locator("#screenImage")).to_be_visible()
    wait_until(page, lambda: len(seen["acks"]) >= 1, "the frame's ack")
    if paused:
        # Stop time: from here the page's timers fire only when the test says.
        context.clock.pause_at(page.evaluate("Date.now()") + 1_000)
    _set_mode(page, "control")
    expect(page.locator('#screenControl .segmented-item[data-control="touch"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    return seen, sockets, page, context


def test_the_loupe_appears_after_150ms_hides_on_lift_and_the_click_is_unchanged(
    authed_page: Page, base_url: str
) -> None:
    seen, _sockets, page, context = _live_touch_view(authed_page, base_url, paused=True)
    loupe = page.locator("#screenLoupe")
    expect(loupe).to_have_js_property("hidden", True)
    expect(loupe).to_have_attribute("aria-hidden", "true")
    expect(loupe).to_have_css("pointer-events", "none")

    finger = _point(page, 0.5, 0.6)
    _pointer(page, "pointerdown", finger)
    context.clock.run_for(149)
    assert _loupe(page)["hidden"], "the loupe is up before 150 ms"
    context.clock.run_for(1)
    shown = _loupe(page)
    assert not shown["hidden"], shown

    # Above the fingertip, centred on it, round, and never under a touch.
    stage = page.evaluate(_STAGE_JS)
    local = {"x": finger["x"] - stage["left"], "y": finger["y"] - stage["top"]}
    assert shown["w"] == shown["h"] > 0, shown
    assert shown["side"] == "above" and shown["y"] + shown["h"] <= local["y"] - _GAP + 1, (shown, local)
    assert abs(shown["x"] + shown["w"] / 2 - local["x"]) <= 1, (shown, local)
    assert shown["pointerEvents"] == "none" and shown["hitsStage"] and not shown["hitsLoupe"], shown
    # 2.5x of the picture as the stage draws it.
    assert abs(float(shown["bgSize"].split("px")[0]) - shown["drawnW"] * 2.5) <= 1, shown

    # Lifting after the loupe came up is still the plain click at the finger.
    _pointer(page, "pointerup", finger)
    assert _loupe(page)["hidden"], "the loupe stays up after the lift"
    context.clock.run_for(1)
    wait_until(page, lambda: len(seen["inputs"]) >= 1, "the click")
    click = seen["inputs"][0]
    assert (click["type"], click["button"]) == ("click", "left"), seen["inputs"]
    assert _near(click["x"], 0.5) and _near(click["y"], 0.6), click
    assert len(seen["inputs"]) == 1, seen["inputs"]

    # A long-press is still the right click, and the loupe makes way for it.
    held = _point(page, 0.3, 0.4)
    _pointer(page, "pointerdown", held)
    context.clock.run_for(150)
    assert not _loupe(page)["hidden"]
    context.clock.run_for(350)
    wait_until(page, lambda: len(seen["inputs"]) >= 2, "the long-press")
    right = seen["inputs"][1]
    assert (right["type"], right["button"]) == ("click", "right"), seen["inputs"]
    assert _near(right["x"], 0.3) and _near(right["y"], 0.4), right
    assert _loupe(page)["hidden"], "the loupe stays up over the right click"
    _pointer(page, "pointerup", held)
    assert len(seen["inputs"]) == 2, seen["inputs"]


def test_the_loupe_is_hidden_in_view_trackpad_with_two_fingers_and_a_stale_picture(
    authed_page: Page, base_url: str
) -> None:
    seen, sockets, page, context = _live_touch_view(authed_page, base_url, paused=True)
    finger = _point(page, 0.5, 0.5)

    def held_down() -> Dict:
        """One finger down for 300 ms: the loupe's state, then lift."""
        _pointer(page, "pointerdown", finger)
        context.clock.run_for(300)
        state = _loupe(page)
        _pointer(page, "pointerup", finger)
        return state

    # Sanity: Touch control shows it, so the cases below are not vacuous.
    _pointer(page, "pointerdown", finger)
    context.clock.run_for(150)
    assert not _loupe(page)["hidden"]
    # A second finger lands: a scroll or pinch, not an aim.
    _pointer(page, "pointerdown", {"x": finger["x"] + 40, "y": finger["y"]}, 2)
    assert _loupe(page)["hidden"], "the loupe stays up with two fingers down"
    _pointer(page, "pointerup", {"x": finger["x"] + 40, "y": finger["y"]}, 2)
    _pointer(page, "pointerup", finger)
    context.clock.run_for(1)

    # Trackpad: the finger moves a cursor, it does not aim.
    page.locator('#screenControl .segmented-item[data-control="trackpad"]').click()
    assert held_down()["hidden"], "the loupe is up in Trackpad"
    page.locator('#screenControl .segmented-item[data-control="touch"]').click()

    # A picture that is not live is not something to aim at.
    sockets[-1].send(json.dumps({"type": "status", "state": "unavailable"}))
    wait_until(
        page,
        lambda: page.evaluate("document.getElementById('screenState').textContent.length > 0"),
        "the stale picture",
    )
    assert held_down()["hidden"], "the loupe is up over a stale picture"
    sockets[-1].send(_frame())
    wait_until(page, lambda: len(seen["acks"]) >= 2, "the live picture back")
    _pointer(page, "pointerdown", finger)
    context.clock.run_for(150)
    assert not _loupe(page)["hidden"]
    _pointer(page, "pointerup", finger)

    # View: touches are only the local zoom and pan.
    _set_mode(page, "view")
    assert held_down()["hidden"], "the loupe is up in View"
    _set_mode(page, "control")
    # Nothing in all that was sent to the PC but the picture's acks.
    assert [m for m in seen["inputs"] if m.get("type") != "click"] == [], seen["inputs"]


def test_the_loupe_follows_a_drag_stays_inside_the_stage_and_the_drag_is_unchanged(
    authed_page: Page, base_url: str
) -> None:
    seen, _sockets, page, _context = _live_touch_view(authed_page, base_url, paused=False)
    start, end = _point(page, 0.2, 0.2), _point(page, 0.7, 0.5)

    # Press, then move past the tap slop: a drag, which holds the loupe up.
    _pointer(page, "pointerdown", start)
    _pointer(page, "pointermove", {"x": start["x"] + 14, "y": start["y"] + 8})
    expect(page.locator("#screenLoupe")).to_have_js_property("hidden", False)
    first = _loupe(page)
    assert first["hitsStage"] and not first["hitsLoupe"], first

    # It follows the finger, still centred above it.
    stage = page.evaluate(_STAGE_JS)
    mid = {"x": (start["x"] + end["x"]) / 2, "y": (start["y"] + end["y"]) / 2}
    _pointer(page, "pointermove", mid)
    followed = _loupe(page)
    assert abs(followed["x"] + followed["w"] / 2 - (mid["x"] - stage["left"])) <= 1, followed
    assert followed["x"] > first["x"], (first, followed)
    _pointer(page, "pointermove", end)

    # Wandering to the stage's edges: it flips under the finger at the top and
    # is pinned inside on every side, never cut off.
    for local, side in (((2, 2), "below"), ((stage["w"] - 2, stage["h"] / 2), "above"),
                        ((2, stage["h"] - 2), "above"), ((stage["w"] / 2, 30), "below")):
        _pointer(page, "pointermove", _on_stage(page, *local))
        edge = _loupe(page)
        assert edge["side"] == side, (local, edge)
        assert 0 <= edge["x"] and edge["x"] + edge["w"] <= edge["stageW"], (local, edge)
        assert 0 <= edge["y"] and edge["y"] + edge["h"] <= edge["stageH"], (local, edge)
    _pointer(page, "pointermove", end)

    # Lift: gone, and the drag the PC got is the one without a loupe.
    _pointer(page, "pointerup", end)
    expect(page.locator("#screenLoupe")).to_have_js_property("hidden", True)
    wait_until(page, lambda: any(m["type"] == "up" for m in seen["inputs"]), "the drag's up")
    drag = seen["inputs"]
    assert drag[0]["type"] == "down" and _near(drag[0]["x"], 0.2) and _near(drag[0]["y"], 0.2), drag
    assert all(m["type"] in ("move", "up") for m in drag[1:]), drag
    assert drag[-1]["type"] == "up" and _near(drag[-1]["x"], 0.7) and _near(drag[-1]["y"], 0.5), drag
