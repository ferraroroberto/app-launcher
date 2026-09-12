"""Regression pin for issue #930 — a keyboard sweep must not storm the PTY.

Every PTY resize SIGWINCHes Claude Code into a full-viewport repaint, and the
copy already in xterm's scrollback survives it, so each resize frame can land
a duplicate of the visible conversation. The iOS keyboard sweep fires
``visualViewport`` resize/scroll events mid-animation, and ``applySize()``
used to forward every intermediate row count straight to the PTY — a single
keyboard cycle produced bursts like ``44 -> 20 -> 6 -> 20 -> 44`` (measured in
a live chief session's transcript), including ``1``- and ``6``-row samples
that are never a real viewport.

The fix keeps xterm's local ``fit()`` immediate (the UI still tracks the
keyboard) but settles the frame that reaches the PTY, and floors it. The
keyboard can't be raised headlessly, so this drives the same code path by
resizing the terminal host synchronously and firing ``resize`` — the event
``applySize()`` listens on — then reads the resize frames the page actually
put on the WebSocket. It pins:

* nothing is sent mid-sweep;
* a sweep that returns to where it started sends nothing at all;
* a sweep that ends somewhere new sends exactly one frame, for the final size;
* a sub-floor sample is clamped before it is sent.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

# Settle window plus generous headroom for a loaded runner: long enough that a
# settled frame has certainly gone out, still far below any test timeout.
_AFTER_SETTLE_MS = 1500

# Record every resize frame the page sends, before any app script runs.
_RECORD_RESIZE_FRAMES = r"""
(() => {
  window.__resizeFrames = [];
  const send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    try {
      const msg = JSON.parse(data);
      if (msg && msg.type === 'resize') {
        window.__resizeFrames.push({ rows: msg.rows, cols: msg.cols });
      }
    } catch (_) { /* not a JSON control frame */ }
    return send.apply(this, arguments);
  };
})();
"""

# Resize the terminal host through each height synchronously, firing the
# window 'resize' applySize() is bound to after every step — the same shape as
# a keyboard sweep's burst of visualViewport events. Returns how many frames
# went out *during* the sweep.
_SWEEP = r"""
(heights) => {
  const host = document.getElementById('terminalHost');
  const before = window.__resizeFrames.length;
  for (const h of heights) {
    host.style.flex = '0 0 ' + h + 'px';
    window.dispatchEvent(new Event('resize'));
  }
  return window.__resizeFrames.length - before;
}
"""


def _frames(page: Page) -> list:
    return page.evaluate("window.__resizeFrames.slice()")


def _clear(page: Page) -> None:
    page.evaluate("window.__resizeFrames.length = 0")


def test_keyboard_sweep_settles_to_one_floored_resize(
    authed_page: Page, base_url: str, browser_name: str, launched_pty_session: str
) -> None:
    if browser_name != "webkit":
        pytest.skip(
            "the in-page terminal is phone-only since #282 — the desktop "
            "row-tap opens a PC mirror window, which never sends resize frames"
        )
    page = authed_page
    page.add_init_script(_RECORD_RESIZE_FRAMES)
    # Open via the session-list row — the phone path. The ?terminal= deep link
    # classifies a loopback open as the PC mirror window (#241), which never
    # sends resize frames at all.
    page.goto(base_url, wait_until="domcontentloaded")
    row = page.locator(
        f'#sessionsList li.session-item[data-session-id="{launched_pty_session}"]'
    )
    row.locator(".session-open").click()
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    # The phone's authoritative first size goes out on WS open, unsettled.
    page.wait_for_function(
        "window.__resizeFrames.length >= 1", timeout=OVERLAY_OPEN_MS
    )

    # Establish a known tall baseline and let it settle out.
    page.evaluate(_SWEEP, [600])
    page.wait_for_timeout(_AFTER_SETTLE_MS)
    _clear(page)

    # 1. Keyboard up and back down: nothing mid-sweep, nothing after it.
    sent_mid_sweep = page.evaluate(_SWEEP, [300, 90, 300, 600])
    assert sent_mid_sweep == 0, (
        f"{sent_mid_sweep} resize frame(s) went out mid-sweep — every "
        "intermediate keyboard sample SIGWINCHes the agent into another "
        "full-viewport repaint (issue #930)"
    )
    page.wait_for_timeout(_AFTER_SETTLE_MS)
    assert _frames(page) == [], (
        f"a sweep that returned to its starting size sent {_frames(page)!r} — "
        "each frame is a repaint that duplicates the conversation in scrollback"
    )

    # 2. Keyboard up and staying up: exactly one frame, for the final size.
    page.evaluate(_SWEEP, [300, 90, 300])
    page.wait_for_timeout(_AFTER_SETTLE_MS)
    settled = _frames(page)
    assert len(settled) == 1, (
        f"expected exactly one settled resize frame, got {settled!r}"
    )
    _clear(page)

    # 3. A sub-floor sample is clamped before it reaches the PTY.
    page.evaluate(_SWEEP, [10])
    page.wait_for_timeout(_AFTER_SETTLE_MS)
    floored = _frames(page)
    min_rows = page.evaluate("import('/static/terminal.js').then(m => m.PTY_MIN_ROWS)")
    assert len(floored) == 1, f"expected one floored frame, got {floored!r}"
    assert min_rows and floored[0]["rows"] == min_rows, (
        f"a 10px host sent rows={floored[0]['rows']} (floor {min_rows!r}) — a "
        "sub-viewport sample reached the PTY"
    )

    page.evaluate("document.getElementById('terminalHost').style.flex = ''")
