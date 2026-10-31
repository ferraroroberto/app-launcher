"""The session overlay keeps its composer on-screen and tappable on a phone.

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
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# Phone portraits: iPhone 15 Pro Max (the WebKit projection's own), a
# 6.1" iPhone, and the SE — the narrowest composer the fleet still targets.
_GEOMETRIES = [(430, 932), (390, 844), (375, 667)]
_BUTTONS = ("composer-mic", "composer-keys", "composer-image", "composer-send")

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


def test_overlay_keeps_composer_reachable_on_a_phone(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    page = authed_page
    if browser_name == "chromium":
        # The phone rules also need a coarse pointer, which the desktop
        # projection lacks.
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    page.set_viewport_size({"width": _GEOMETRIES[0][0], "height": _GEOMETRIES[0][1]})
    page.goto(f"{base_url}/?terminal={launched_pty_session}", wait_until="load")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    for geometry in _GEOMETRIES:
        page.set_viewport_size({"width": geometry[0], "height": geometry[1]})
        for mode in ("terminal", "chat"):
            _check_mode(page, mode, geometry)

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
