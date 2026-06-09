"""Regression pin for issue #135 — keep the prompt above the keyboard.

On iOS the software keyboard shrinks ``window.visualViewport.height`` but
NOT the layout viewport, so the ``position:fixed; inset:0`` terminal
overlay keeps covering the whole screen *behind* the keyboard and the
active prompt row renders hidden under it. ``keyboardOverlayHeight()`` in
``terminal.js`` decides when to pin the overlay to the visual-viewport
height (keyboard up) versus release it to the CSS ``100dvh`` full height
(keyboard down / minor chrome changes); ``applySize()`` then re-fits
xterm to the smaller box.

This exercises the pure helper directly (the keyboard can't be raised in
a headless browser), plus the CSS over-constraint contract it depends
on: with ``position:fixed; inset:0``, setting ``height`` + ``bottom:auto``
must actually shrink the overlay (otherwise the pin is a no-op and the
prompt stays hidden). It pins:

* equal layout/visual heights → no override (``null``);
* a minor chrome shrink (<120px) → no override;
* a keyboard-sized shrink → override == visual height (rounded);
* invalid inputs → no override;
* applying the height + ``bottom:auto`` shrinks the overlay, and clearing
  both restores it to the full viewport height;
* applying ``top`` moves the overlay down to track ``visualViewport.offsetTop``
  (issue #135 reopen — iOS shifts the visual viewport down to sweep the
  focused line into view; without matching that offset the overlay slides off
  the top and a band of the page shows through above the keyboard), and
  clearing ``top`` returns it to the ``inset:0`` origin.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

_PROBE = r"""
async () => {
  const mod = await import('/static/terminal.js');
  const kbh = mod.keyboardOverlayHeight;
  const full = 900;
  const helper = {
    noKeyboard: kbh(full, full),          // equal → null
    chrome: kbh(full, full - 80),         // <120px chrome shrink → null
    keyboard: kbh(full, full - 340),      // keyboard-sized shrink → 560
    invalid: kbh(0, 500),                 // bad layout height → null
    rounds: kbh(full, 560.6),             // fractional visual height → 561
  };

  // CSS contract: with position:fixed; inset:0, height + bottom:auto must
  // win the over-constraint and actually shrink the box; clearing both
  // restores full-viewport height. And `top` must move the box down to
  // track visualViewport.offsetTop (issue #135 reopen) — without it the
  // overlay slides off the top and the page shows through above the
  // keyboard; clearing `top` returns it to the inset:0 origin.
  const ov = document.getElementById('terminalOverlay');
  const wasHidden = ov.hidden;
  ov.hidden = false;
  ov.style.height = '300px';
  ov.style.bottom = 'auto';
  ov.style.top = '40px';
  const constrained = Math.round(ov.getBoundingClientRect().height);
  const offsetTop = Math.round(ov.getBoundingClientRect().top);
  ov.style.height = '';
  ov.style.bottom = '';
  ov.style.top = '';
  const released = Math.round(ov.getBoundingClientRect().height);
  const releasedTop = Math.round(ov.getBoundingClientRect().top);
  ov.hidden = wasHidden;

  return {
    ...helper, constrained, offsetTop, released, releasedTop,
    innerH: window.innerHeight,
  };
}
"""


def test_keyboard_overlay_height(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    r = authed_page.evaluate(_PROBE)

    assert r["noKeyboard"] is None, (
        "equal layout/visual heights wrongly produced an override "
        f"({r['noKeyboard']!r}) — the overlay would shrink with no keyboard up"
    )
    assert r["chrome"] is None, (
        f"a {80}px chrome shrink produced an override ({r['chrome']!r}) — "
        "URL-bar/home-indicator changes must stay on the CSS 100dvh path"
    )
    assert r["keyboard"] == 560, (
        f"keyboard-sized shrink returned {r['keyboard']!r}, expected 560 — "
        "the overlay won't drop its bottom edge to the top of the keyboard"
    )
    assert r["invalid"] is None, (
        f"invalid layout height returned {r['invalid']!r}, expected null"
    )
    assert r["rounds"] == 561, (
        f"fractional visual height returned {r['rounds']!r}, expected 561 "
        "(rounded) — a sub-pixel height leaves a hairline gap"
    )
    assert r["constrained"] == 300, (
        f"overlay height was {r['constrained']}px after height+bottom:auto, "
        "expected 300 — inset:0 won the over-constraint, so the pin is a no-op "
        "and the prompt stays hidden behind the keyboard"
    )
    assert r["offsetTop"] == 40, (
        f"overlay top was {r['offsetTop']}px after setting top:40px, expected "
        "40 — the overlay won't follow visualViewport.offsetTop, so it slides "
        "off the top and a band of the page shows through above the keyboard"
    )
    assert r["released"] == r["innerH"], (
        f"overlay was {r['released']}px after clearing the override, expected "
        f"the full {r['innerH']}px viewport — it won't expand back when the "
        "keyboard hides"
    )
    assert r["releasedTop"] == 0, (
        f"overlay top was {r['releasedTop']}px after clearing top, expected 0 "
        "— it won't return to the inset:0 origin when the keyboard hides, "
        "leaving the terminal shifted down"
    )
