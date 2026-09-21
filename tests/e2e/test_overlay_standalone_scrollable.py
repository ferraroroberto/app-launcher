"""Regression pin for #1099 avenue B: the session overlay keeps the document
1px scrollable in the installed-PWA shell.

The vendored nav contract (``_vendored/nav/nav-tabs.css``, standalone block)
makes ``.app`` the fixed scroller and keeps the *document* technically
scrollable with an inert ``body::after { height: calc(100dvh + 1px) }``
spacer, because iOS standalone only holds the layout viewport expanded to the
full physical screen while the document has scrollable overflow. Opening the
session overlay used to destroy that twice over — ``body.terminal-open``'s
``overflow: hidden`` (propagated to the viewport) and ``lockBodyScroll()``'s
``position: fixed`` pin (body and its spacer out of flow) — and the residual
~55-63 CSS px band below the composer, common to Terminal and Chat, is the
contracted viewport that leaves behind.

**What this can and cannot prove.** Neither headless engine implements
``display-mode: standalone`` (nor can CDP emulate it), so the test *projects*
the shell: every media condition naming it is rewritten to an always-true one,
in the parsed stylesheets and in ``matchMedia``, and then the real app runs
against real geometry. It pins that the overlay leaves the document
scrollable, that ``.app`` still carries the lock, that the page behind does
not scroll, and that all four composer buttons stay on-screen and hit-testable
in both modes across phone geometries. ``env(safe-area-inset-bottom)`` is
``0px`` here and no engine reproduces iOS's viewport contraction, so a green
run does **not** prove the band is gone — that is a device reading.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

_STANDALONE = "(display-mode: standalone)"
_ALWAYS = "(min-width: 0px)"

# matchMedia must answer before the app's modules evaluate: terminal.js reads
# its standalone-shell query at import.
_PATCH_MATCH_MEDIA = f"""
(() => {{
  const orig = window.matchMedia.bind(window);
  window.matchMedia = (q) => orig(String(q).split('{_STANDALONE}').join('{_ALWAYS}'));
}})();
"""

_PROJECT_STANDALONE_CSS = f"""
() => {{
  let rewritten = 0;
  const walk = (rules) => {{
    for (const rule of rules || []) {{
      if (rule.media && rule.media.mediaText.includes('{_STANDALONE}')) {{
        rule.media.mediaText = rule.media.mediaText.split('{_STANDALONE}').join('{_ALWAYS}');
        rewritten += 1;
      }}
      if (rule.cssRules && rule.cssRules.length) walk(rule.cssRules);
    }}
  }};
  for (const sheet of document.styleSheets) {{
    try {{ walk(sheet.cssRules); }} catch (_) {{ /* cross-origin */ }}
  }}
  return rewritten;
}}
"""

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
    appOverflowY: getComputedStyle(document.querySelector('main.app')).overflowY,
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

    assert m["bodyPosition"] != "fixed", (
        f"{where}: body is pinned position:fixed in the standalone shell — "
        "lockBodyScroll() takes body::after out of flow and the layout "
        "viewport contracts (#1099)."
    )
    assert m["scrollRange"] >= 1, (
        f"{where}: the document has no scrollable overflow with the overlay "
        f"open (range {m['scrollRange']}px); the vendored 1px spacer must "
        "survive the overlay (#1099 avenue B)."
    )
    assert m["appOverflowY"] == "hidden", (
        f"{where}: .app must stay overflow:hidden behind the overlay — it is "
        f"the standalone lock now that body is not pinned (got {m['appOverflowY']})."
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


def test_overlay_keeps_document_scrollable_in_standalone_shell(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    page = authed_page
    if browser_name == "chromium":
        # The shell's query also needs a coarse pointer, which the desktop
        # projection lacks; emulate touch so both engines run the shell.
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    page.add_init_script(_PATCH_MATCH_MEDIA)
    page.set_viewport_size({"width": _GEOMETRIES[0][0], "height": _GEOMETRIES[0][1]})
    page.goto(f"{base_url}/?terminal={launched_pty_session}", wait_until="load")
    assert page.evaluate(_PROJECT_STANDALONE_CSS) >= 1, (
        "no display-mode: standalone media rule found to project — the "
        "vendored shell block moved; this test would silently run the "
        "browser-tab layout instead"
    )
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    assert page.evaluate(
        "() => matchMedia('(pointer: coarse) and (max-width: 520px) "
        "and (display-mode: standalone)').matches"
    ), "the standalone-shell projection did not take on this engine"

    for geometry in _GEOMETRIES:
        page.set_viewport_size({"width": geometry[0], "height": geometry[1]})
        for mode in ("terminal", "chat"):
            _check_mode(page, mode, geometry)

    # No background scroll. The document is now 1px scrollable by design, so
    # what keeps a gesture from reaching it is the overlay refusing to chain
    # (overflow hidden + overscroll-behavior contain) — the contract's "no
    # touch can reach it". Pinned structurally on both engines...
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
