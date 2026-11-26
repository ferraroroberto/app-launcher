"""Legs of ``test_overlay_phone_composer.py``: the terminal takes back the room
the phone keyboard took, and the PC mirror keeps its last row in view.

A full-screen agent's PTY is not resized again by a shrink, the canvas pans. A
terminal first sized while the keyboard was up under Chat must still grow when
it closes.

The phone keyboard is a shorter viewport (``interactive-widget=
resizes-content``), which is what a resize of the page reproduces.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Error, Page, expect

from tests.e2e.fixtures.ui import (
    RECORD_RESIZE_FRAMES,
    open_session_row,
    stub_session_mirror,
)
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

_LONG = "\n".join(f"line {i} of a long message" for i in range(12))

# The mirror window docks no composer, so there is nothing to overlap there.
_GEOMETRY = """() => {
  const box = (sel) => document.querySelector(sel).getBoundingClientRect();
  const host = box('#terminalHost');
  const screen = box('#terminalHost .xterm-screen');
  const bar = box('#terminalComposeBar');
  return {
    below: screen.bottom - host.bottom,
    top: screen.top - host.top,
    overlap: bar.height ? host.bottom - bar.top : 0,
  };
}"""

# The last row ends inside the box, and the box ends above the composer.
_VISIBLE = "g.below <= 0.5 && g.overlap <= 0.5"
# ...and the canvas covers the box too: no dead band left under it (a row is 15px).
_FILLED = _VISIBLE + " && g.below > -20"


def _expect_geometry(page: Page, rule: str, what: str) -> None:
    try:
        page.wait_for_function(
            f"() => {{ const g = ({_GEOMETRY})(); return {rule}; }}", timeout=5000
        )
    except Error:
        pytest.fail(f"{what}: {page.evaluate(_GEOMETRY)}")


def leg_terminal_grows_back_after_the_keyboard(
    page: Page, base_url: str, sid: str, width: int, height: int
) -> None:
    """A full-screen terminal first opened from Chat with the keyboard up. The
    caller reports the session as a full-screen agent's."""
    page.add_init_script(RECORD_RESIZE_FRAMES)
    page.set_viewport_size({"width": width, "height": height})
    stub_session_mirror(page)
    page.goto(base_url, wait_until="domcontentloaded")
    # A remembered Terminal mode would size the terminal before the keyboard is up.
    page.evaluate("localStorage.removeItem('launcher.sessionModes')")
    row = page.locator(f'#sessionsList li.session-item[data-session-id="{sid}"]')
    open_session_row(page, row, "chat")

    # Type a long message with the keyboard up, then switch to Terminal.
    page.set_viewport_size({"width": width, "height": 480})
    page.locator("#chatComposeBar .composer-input").fill(_LONG)
    page.locator("#sessionModeTerminal").click()
    expect(page.locator("#terminalHost .xterm-screen")).to_be_visible(
        timeout=OVERLAY_OPEN_MS
    )
    page.wait_for_function("window.__resizeFrames.length >= 1", timeout=OVERLAY_OPEN_MS)
    _expect_geometry(page, _FILLED, "keyboard still up")

    # The keyboard closes: the terminal takes the room back, PTY included.
    page.set_viewport_size({"width": width, "height": height})
    _expect_geometry(page, _FILLED, "keyboard closed")
    page.wait_for_function(
        "window.__resizeFrames.length >= 2 && "
        "window.__resizeFrames.at(-1).rows > window.__resizeFrames[0].rows"
    )
    # The resize may draw nothing: the canvas must not stay hidden waiting for it.
    expect(page.locator("#terminalHost .xterm")).to_be_visible(timeout=3000)
    frames = page.evaluate("window.__resizeFrames.length")

    # A growing composer pans the canvas; a cleared one gives the rows back.
    composer = page.locator("#terminalComposeBar .composer-input")
    composer.fill(_LONG)
    _expect_geometry(page, _VISIBLE + " && g.top < 0", "composer grown")
    composer.fill("")
    _expect_geometry(page, _FILLED, "composer cleared")
    assert page.evaluate("window.__resizeFrames.length") == frames, (
        "a composer growing and clearing resized the full-screen agent's PTY"
    )


def leg_mirror_keeps_the_last_row_in_view(page: Page, base_url: str, sid: str) -> None:
    # The PC mirror never resizes the PTY, so a short window can only pan. The
    # ?terminal= link on a loopback origin is the mirror window.
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    expect(page.locator("#terminalHost .xterm-screen")).to_be_visible(
        timeout=OVERLAY_OPEN_MS
    )
    page.set_viewport_size({"width": 1280, "height": 360})
    _expect_geometry(page, _VISIBLE + " && g.top < 0", "short mirror window")
