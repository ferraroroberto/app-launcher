"""The session overlay keeps its composer and its bar on-screen on a phone.

Opening the overlay pins ``body`` (``lockBodyScroll()``, the plain pin the
vendored nav asks of an app that pins its body). The nav's
``html { min-height: calc(100dvh + 1px) }`` keeps the document scrollable
while the body is out of flow, so iOS never contracts the layout viewport and
leaves a dead band under the composer (#1099). This pins, at three phone
geometries and in both modes, that the body is pinned, the document still has
scrollable range, and all four composer buttons are on-screen and
hit-testable, and that the overlay refuses to chain a scroll to the document.

``env(safe-area-inset-bottom)`` is ``0px`` here and no engine reproduces iOS's
viewport contraction, so a green run does **not** prove the band is gone: that
is a device reading. Chromium gets touch emulation so the phone rules
(``pointer: coarse``) apply.

The same test then sweeps the overlay's top bar, one page load per leg on the
same page and PTY, each leg a former test kept as a named helper:

* the model pill (#1383), formerly ``test_terminal_model_pill.py``: left of the
  context ring and level with it, absent (not blank) with no model, and at
  360px the title takes the squeeze while the bar does not overflow;
* the model chip's picker: a pick switches only on the server's confirmation,
  and an agent with no in-session switch keeps a chip that says why;
* the bar's row fit (#514, #981), formerly ``test_terminal_bar_overflow.py``:
  every control inside the viewport with no scroller, at 320px and at the
  projection's default width. iPhone only, where that width reproduces.

The bar legs stub the context ring's route with a number (a stub-child PTY
paints no statusline, and the ring only draws with one) and the sessions
list's ``model`` field (the derivation is pinned by
``tests/unit/sessions/test_session_model.py``), so they measure the worst
case: ring, toggle, 🔊 and ⋮ all showing.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# Phone portraits: iPhone 15 Pro Max (the WebKit projection's own), a
# 6.1" iPhone, and the SE — the narrowest composer the fleet still targets.
_GEOMETRIES = [(430, 932), (390, 844), (375, 667)]
_BUTTONS = ("composer-mic", "composer-keys", "composer-image", "composer-send")

# The model pill's narrow phone, and the bar's narrowest one (iPhone SE 1st-gen).
_PILL_PHONE = {"width": 360, "height": 740}
_NARROW_VIEWPORT = {"width": 320, "height": 640}

_MEASURE = """
(mount) => {
  const vh = window.innerHeight, vw = window.innerWidth;
  const buttons = ['composer-mic', 'composer-keys', 'composer-image', 'composer-send']
    .map((cls) => {
      const b = document.querySelector('#' + mount + ' .' + cls);
      const r = b.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {
        cls,
        top: r.top, bottom: r.bottom, left: r.left, right: r.right,
        width: r.width, height: r.height,
        hittable: !!hit && (hit === b || b.contains(hit)),
      };
    });
  const se = document.scrollingElement;
  return {
    vh, vw, buttons,
    scrollRange: se.scrollHeight - se.clientHeight,
    bodyPosition: getComputedStyle(document.body).position,
  };
}
"""


class _BarStubs:
    """The context ring's percentage and every session's ``model``, switched per leg."""

    def __init__(self, page: Page) -> None:
        self.percent = 42
        self.model: Optional[str] = None
        self.agent: Optional[str] = None
        page.route(re.compile(r".*/api/claude-code/sessions/[^/]+/context$"), self._context)
        page.route(re.compile(r".*/api/claude-code/sessions$"), self._sessions)

    def _context(self, route) -> None:
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"available": True, "percent": self.percent, "reason": None}),
        )

    def _sessions(self, route) -> None:
        resp = route.fetch()
        body = resp.json()
        for sess in body.get("sessions", []):
            sess["model"] = self.model
            if self.agent:
                sess["agent"] = self.agent
        route.fulfill(response=resp, json=body)


def _open_by_row(page: Page, base_url: str, sid: str) -> None:
    # The session-list row tap is the phone path: the ?terminal= deep link
    # would classify this loopback open as a PC mirror window (#241) and hide
    # parts of the bar, undercounting the row's real phone width.
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator(
        f'#sessionsList li.session-item[data-session-id="{sid}"] .session-open'
    ).click()
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)


def _check_mode(page: Page, mode: str, geometry: tuple) -> None:
    mount = "chatComposeBar" if mode == "chat" else "terminalComposeBar"
    seg = "#sessionModeChat" if mode == "chat" else "#sessionModeTerminal"
    page.locator(seg).click()
    expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", mode)
    # The loopback harness can hide a mount (PC mirror) or the mic (no voice
    # backend); un-hide both so the full 2x2 grid is what gets measured.
    page.evaluate(
        """(mount) => {
          const host = document.getElementById(mount);
          host.hidden = false;
          host.querySelectorAll('.compose-tools button').forEach((b) => {
            b.hidden = false; b.style.display = '';
          });
        }""",
        mount,
    )
    expect(page.locator(f"#{mount} .compose-bar")).to_be_visible()
    m = page.evaluate(_MEASURE, mount)
    where = f"{mode} @ {geometry[0]}x{geometry[1]}"

    assert m["bodyPosition"] == "fixed", (
        f"{where}: body is not pinned behind the overlay (got {m['bodyPosition']})."
    )
    assert m["scrollRange"] >= 1, (
        f"{where}: the document has no scrollable overflow with the body pinned "
        f"(range {m['scrollRange']}px); the nav's html min-height must hold "
        "(#1099)."
    )
    for b in m["buttons"]:
        assert b["width"] >= 40 and b["height"] >= 40, f"{where}: {b} collapsed"
        assert b["top"] >= 0 and b["bottom"] <= m["vh"] + 0.5, (
            f"{where}: {b['cls']} is off-screen vertically: {b} (viewport {m['vh']})"
        )
        assert b["left"] >= 0 and b["right"] <= m["vw"] + 0.5, (
            f"{where}: {b['cls']} is off-screen horizontally: {b}"
        )
        assert b["hittable"], f"{where}: {b['cls']} is covered at its center: {b}"


def _check_no_scroll_chaining(page: Page, browser_name: str) -> None:
    # No background scroll. The document stays 1px scrollable by design, so
    # what keeps a gesture from reaching it is the overlay refusing to chain
    # (overflow hidden + overscroll-behavior contain). Pinned structurally
    # on both engines...
    guards = page.evaluate(
        """() => {
          const o = getComputedStyle(document.getElementById('terminalOverlay'));
          // Playwright's Windows WebKit build has no overscroll-behavior at
          // all (iOS Safari 16+ does); there the declaration is unreadable,
          // not absent, so only engines that implement it are asserted on.
          const supported = CSS.supports('overscroll-behavior', 'contain');
          return { overflow: o.overflowY, position: o.position,
                   overscroll: supported ? o.getPropertyValue('overscroll-behavior-y') : 'contain' };
        }"""
    )
    assert guards == {"overflow": "hidden", "overscroll": "contain", "position": "fixed"}, (
        f"the overlay no longer stops scroll chaining to the document: {guards}"
    )
    if browser_name == "webkit":
        return  # ...and behaviourally where the engine can wheel (mobile WebKit can't).
    page.evaluate("() => window.scrollTo(0, 0)")
    box = page.locator("#terminalOverlay .terminal-bar").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 400)
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.scrollY") == 0, (
        "a wheel over the overlay scrolled the document behind it"
    )


def _leg_model_pill_sits_left_of_the_ring_and_the_header_still_fits(
    page: Page, base_url: str, sid: str, stubs: _BarStubs
) -> None:
    """#1383: a neutral pill left of the context ring names the session's model."""
    page.set_viewport_size(_PILL_PHONE)
    stubs.model, stubs.percent = "Opus", 42
    _open_by_row(page, base_url, sid)

    pill = page.locator("#terminalModel")
    ring = page.locator("#contextRing")
    expect(pill).to_be_visible()
    expect(pill).to_have_text("Opus")
    expect(ring).to_be_visible()
    # Every control showing, at a current iPhone's width: the pill reads in full.
    page.set_viewport_size({"width": 390, "height": 740})
    wide = page.evaluate(
        "() => { const p = document.querySelector('#terminalModelLabel');"
        " return p.scrollWidth > p.clientWidth; }"
    )
    assert not wide, "the model pill clips its own text at 390px"
    # Then the narrow phone: there is no room left for the title, and the bar
    # must still not overflow.
    page.set_viewport_size(_PILL_PHONE)

    order = page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar-actions > *", "els => els.map(el => el.id)"
    )
    assert order[:2] == ["terminalModel", "contextRing"], order

    geo = page.evaluate(
        """() => {
          const r = (s) => document.querySelector(s).getBoundingClientRect();
          const bar = document.querySelector('#terminalOverlay .terminal-bar');
          const group = document.querySelector('#terminalOverlay .terminal-bar-actions');
          return {
            vw: window.innerWidth,
            pill: r('#terminalModel'), ring: r('#contextRing'),
            title: r('#terminalTitle'), menu: r('#terminalMenu'),
            back: r('#terminalBack'),
            barScroll: bar.scrollWidth, barClient: bar.clientWidth,
            groupScroll: group.scrollWidth, groupClient: group.clientWidth,
          };
        }"""
    )
    assert geo["pill"]["right"] <= geo["ring"]["left"], "the pill is not left of the ring"
    assert abs(
        (geo["pill"]["top"] + geo["pill"]["bottom"]) / 2
        - (geo["ring"]["top"] + geo["ring"]["bottom"]) / 2
    ) <= 2, "the pill is not level with the ring"
    # No overflow at 360px: the bar, the group and the right-most control.
    assert geo["barScroll"] <= geo["barClient"] + 1, geo
    assert geo["groupScroll"] <= geo["groupClient"] + 1, geo
    assert geo["menu"]["right"] <= geo["vw"], f"⋮ pushed off-screen: {geo['menu']}"
    assert geo["back"]["left"] >= 0
    # The title is what gave way, and it truncates rather than wraps.
    assert geo["title"]["right"] <= geo["pill"]["left"] + 1
    expect(page.locator("#terminalTitle")).to_have_css("text-overflow", "ellipsis")
    expect(page.locator("#terminalTitle")).to_have_css("white-space", "nowrap")


def _leg_no_pill_when_the_model_is_unknown(
    page: Page, base_url: str, sid: str, stubs: _BarStubs
) -> None:
    page.set_viewport_size(_PILL_PHONE)
    stubs.model, stubs.percent = None, 42
    _open_by_row(page, base_url, sid)

    expect(page.locator("#contextRing")).to_be_visible()
    pill = page.locator("#terminalModel")
    expect(pill).to_be_hidden()
    expect(pill).to_have_text("")


def _leg_model_chip_switches_through_the_picker(
    page: Page, base_url: str, sid: str, stubs: _BarStubs
) -> None:
    """A tap on the chip opens the picker with the current model checked; a
    pick changes the chip only on the server's confirmation, and an
    unconfirmed one keeps the old model and says so. The switch itself (the
    agent's /model and its dialog) is pinned by
    ``tests/unit/board/test_model_switch_route.py``."""
    page.set_viewport_size({"width": 390, "height": 844})
    stubs.model, stubs.agent = "Opus", None
    _open_by_row(page, base_url, sid)
    posted = []
    answer = {"outcome": "switched", "model": "Sonnet", "confirmed_dialog": True, "detail": None}

    def switch(route) -> None:
        posted.append(route.request.post_data_json)
        if answer["outcome"] == "switched":
            stubs.model = answer["model"]  # the agent now reports it in the list too
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, **answer}))

    page.route(re.compile(r".*/api/claude-code/sessions/[^/]+/model$"), switch)
    chip = page.locator("#terminalModel")
    sheet = page.locator("#modelSwitchSheet")
    rows = sheet.locator(".model-switch-pick")

    chip.click()
    expect(sheet).to_be_visible()
    # The same list and marks as the Life OS resume Model row: Haiku leads, and
    # the running model carries "Its own" and the check.
    expect(rows.locator(".settings-row-label")).to_have_text(["Haiku", "Sonnet", "Opus", "Fable"])
    opus = rows.filter(has_text="Opus")
    expect(opus).to_have_attribute("aria-current", "true")
    expect(opus.locator(".settings-row-value")).to_have_text("Its own")
    expect(rows.filter(has_text="Sonnet").locator(".settings-row-value")).to_have_count(0)
    rows.filter(has_text="Sonnet").click()
    expect(sheet).to_be_hidden()
    expect(chip).to_have_text("Sonnet")
    assert posted == [{"model": "sonnet"}]

    answer.update(model="Haiku")
    chip.click()
    rows.filter(has_text="Haiku").click()
    expect(chip).to_have_text("Haiku")
    assert posted[-1] == {"model": "haiku"}
    answer.update(model="Sonnet")
    chip.click()
    rows.filter(has_text="Sonnet").click()
    expect(chip).to_have_text("Sonnet")

    answer.update(outcome="unconfirmed", model="Fable", detail="Claude didn't report the switch to Fable")
    chip.click()
    rows.filter(has_text="Fable").click()
    expect(chip).to_have_attribute("data-switch", "unconfirmed")
    expect(chip).to_have_text("Sonnet")
    expect(page.locator("#toast")).to_contain_text("not confirmed")

    stubs.model, stubs.agent = "Opus", "pi"
    _open_by_row(page, base_url, sid)
    expect(chip).to_have_attribute("aria-disabled", "true")
    chip.dispatch_event("click")  # a tap still lands; Playwright refuses aria-disabled
    expect(page.locator("#toast")).to_contain_text("can't switch model")
    expect(sheet).to_be_hidden()
    stubs.agent = None


def _leg_terminal_bar_buttons_stay_within_viewport(
    page: Page, base_url: str, sid: str, stubs: _BarStubs
) -> None:
    """#514: at 320px every bar control lands fully inside the viewport.

    `.terminal-bar-actions` once had no `min-width: 0`, so as a flex item of
    the row-flex `.terminal-bar` it could never shrink below its buttons'
    summed width, and the last button was pushed past the screen edge, where
    `.terminal-overlay { overflow: hidden }` silently clipped it. Since #980
    and #981 the bar is ‹ Back · title · ring · Terminal⇄Chat · 🔊 · ⋮ and
    `#terminalOverlay .terminal-bar-actions` is `overflow-x: visible` (the
    Life OS bars that share the class keep scrolling, #886). #1223 put the
    context ring first in the group, a real 44px target like its neighbours.
    """
    page.set_viewport_size(_NARROW_VIEWPORT)
    stubs.model, stubs.percent = None, 42
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    viewport_width = page.evaluate("window.innerWidth")
    assert viewport_width == _NARROW_VIEWPORT["width"]

    # .terminal-bar / .terminal-bar-actions are shared classes with the Life
    # OS doc-browser bar (#lifeOsBrowser) — scope to #terminalOverlay so the
    # measurement targets the actual open terminal, not the other (hidden,
    # zero-size) bar sharing the same class names. The context ring draws
    # once its stubbed route has answered, and says what it shows.
    ring = page.locator("#contextRing")
    expect(ring).to_be_visible()
    expect(ring).to_have_attribute("aria-label", "Context window 42% used")
    boxes = page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar-actions .term-btn, #sessionMode",
        "els => els.map(el => el.getBoundingClientRect())",
    )
    assert len(boxes) == 3, "expected the context ring, ⋮ and the mode toggle in the group"
    for box in boxes:
        assert box["left"] >= 0, f"button left edge {box['left']} clipped before the viewport"
        assert box["right"] <= viewport_width, (
            f"button right edge {box['right']} overflows viewport width {viewport_width}"
        )

    # Back anchors the bar's left edge and ⋮ its right edge (#981); the
    # ring (#1223) and the mode toggle (#982) sit between them, on-screen.
    for selector in ("#terminalBack", "#contextRing", "#sessionMode", "#terminalMenu"):
        box = page.eval_on_selector(selector, "el => el.getBoundingClientRect()")
        assert box["left"] >= 0
        assert box["right"] <= viewport_width

    # The bar is a cluster, so the ring's 44px is real geometry (design.md
    # touch targets), not an expanded hit area overlapping the toggle.
    ring_box = page.eval_on_selector("#contextRing", "el => el.getBoundingClientRect()")
    toggle_box = page.eval_on_selector("#sessionMode", "el => el.getBoundingClientRect()")
    assert ring_box["width"] >= 44 and ring_box["height"] >= 44, f"ring target {ring_box}"
    assert ring_box["right"] <= toggle_box["left"], "the ring overlaps the mode toggle"

    # A tap reads the number out.
    ring.click()
    expect(page.locator("#toast")).to_contain_text("Context window 42% used")


def _leg_terminal_bar_fits_at_once_on_default_phone(
    page: Page, base_url: str, sid: str, stubs: _BarStubs, viewport: dict
) -> None:
    """Every bar button fits the default iPhone projection without scrolling.

    The second half of #514: with the Back button back at the uniform 44px and
    uniform 6px gaps, the full row must be fully visible at once on the
    suite's default iPhone 15 Pro Max (430px) viewport, with no internal
    scrolling and no clipped button. Five controls since #1223 (Back, the
    context ring, the Terminal⇄Chat toggle, Read aloud, ⋮ menu).
    """
    page.set_viewport_size(viewport)
    stubs.model, stubs.percent = None, 87
    _open_by_row(page, base_url, sid)

    viewport_width = page.evaluate("window.innerWidth")

    # The context ring (#1223) is drawn, here at a danger-tier 87%.
    ring = page.locator("#contextRing")
    expect(ring).to_be_visible()
    expect(ring).to_have_attribute("data-tier", "danger")

    # Equal-size contract: the Back button is the same 44px target as every
    # other bar button (the #496 64px widening is what tipped the row over),
    # the ring included.
    widths = page.evaluate(
        "() => ['#terminalBack', '#contextRing', '#terminalMenu']"
        ".map(s => document.querySelector(s).getBoundingClientRect().width)"
    )
    assert max(widths) - min(widths) <= 1, f"bar buttons unequal widths: {widths}"
    assert min(widths) >= 44, f"a bar button under the 44px target: {widths}"
    # The toggle and the ring are the same 44px tall as their neighbours
    # (#982, #1223); the ring leads the group, then the toggle.
    toggle = page.eval_on_selector("#sessionMode", "el => el.getBoundingClientRect()")
    assert abs(toggle["height"] - widths[0]) <= 1, f"toggle height {toggle['height']} vs 44px controls"
    ring_box = page.eval_on_selector("#contextRing", "el => el.getBoundingClientRect()")
    assert abs(ring_box["height"] - widths[0]) <= 1, f"ring height {ring_box['height']} vs 44px controls"
    # The model pill (#1383) leads the group when a model is known; with none
    # it is hidden and not part of the visible order.
    order = page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar-actions > *:not([hidden])",
        "els => els.map(el => el.id)",
    )
    assert order[:2] == ["contextRing", "sessionMode"], order

    # The actions group must not need its scroller on this width…
    group = page.eval_on_selector(
        "#terminalOverlay .terminal-bar-actions",
        "g => ({scrollWidth: g.scrollWidth, clientWidth: g.clientWidth})",
    )
    assert group["scrollWidth"] <= group["clientWidth"] + 1, (
        f"actions group scrolls on the default phone width: {group}"
    )

    # …and every button — without any scrolling — sits fully on-screen.
    buttons = page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar .term-btn",
        "els => els.map(el => ({id: el.id, hidden: el.hidden,"
        " box: el.getBoundingClientRect()}))",
    )
    visible = [b for b in buttons if not b["hidden"]]
    assert len(visible) == 3, (
        f"expected all 3 bar buttons visible, hidden: "
        f"{[b['id'] for b in buttons if b['hidden']]}"
    )
    for b in visible:
        assert b["box"]["left"] >= 0, f"{b['id']} left edge {b['box']['left']} clipped"
        assert b["box"]["right"] <= viewport_width, (
            f"{b['id']} right edge {b['box']['right']} overflows viewport {viewport_width}"
        )


def test_overlay_keeps_composer_reachable_on_a_phone(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    page = authed_page
    sid = launched_pty_session
    default_viewport = dict(page.viewport_size)
    stubs = _BarStubs(page)
    if browser_name == "chromium":
        # The phone rules also need a coarse pointer, which the desktop
        # projection lacks.
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    page.set_viewport_size({"width": _GEOMETRIES[0][0], "height": _GEOMETRIES[0][1]})
    page.goto(f"{base_url}/?terminal={sid}", wait_until="load")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    for geometry in _GEOMETRIES:
        page.set_viewport_size({"width": geometry[0], "height": geometry[1]})
        for mode in ("terminal", "chat"):
            _check_mode(page, mode, geometry)
    _check_no_scroll_chaining(page, browser_name)

    _leg_model_pill_sits_left_of_the_ring_and_the_header_still_fits(page, base_url, sid, stubs)
    _leg_no_pill_when_the_model_is_unknown(page, base_url, sid, stubs)
    _leg_model_chip_switches_through_the_picker(page, base_url, sid, stubs)
    if browser_name == "webkit":
        # Phone-width row fit only reproduces under the iPhone projection.
        _leg_terminal_bar_buttons_stay_within_viewport(page, base_url, sid, stubs)
        _leg_terminal_bar_fits_at_once_on_default_phone(
            page, base_url, sid, stubs, default_viewport
        )
