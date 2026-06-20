"""Regression pin for issue #264 — pan, don't reflow, a full-screen TUI.

On iPhone the software keyboard shrinks ``visualViewport.height``. For an
inline agent (Claude) ``applySize()`` re-fits xterm to the smaller box
(issue #135). For a full-screen *differential* agent (Codex/ratatui) that
reflow is harmful: it changes the PTY row count, which SIGWINCHes the agent
into repainting its whole frame on every keyboard open/close — the visible
"refreshment" the user reported. The fix keeps the PTY at its stable size
and instead *pans* the fixed canvas up so the bottom row (the prompt) stays
above the keyboard. ``terminalPanY()`` in ``terminal.js`` computes that shift.

The keyboard can't be raised in a headless browser, so this exercises the
pure helper directly plus the CSS pan contract it depends on: inside an
``overflow:hidden`` host, translating a taller child up by the helper's
value must drop the child's bottom edge to the host's bottom edge (so the
prompt lands just above where the keyboard would be), and clamp at 0 so a
canvas already shorter than the box never shifts down (which would expose a
gap below the prompt).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

_PROBE = r"""
async () => {
  const mod = await import('/static/terminal.js');
  const pan = mod.terminalPanY;
  const helper = {
    overflow: pan(900, 560),     // 340px taller than the box → pan 340
    exact: pan(560, 560),        // canvas == box → no pan
    shorter: pan(400, 560),      // canvas shorter than box → clamp 0
    rounds: pan(560.6, 200),     // fractional content height → 361 (rounded)
    invalidContent: pan(0, 560), // bad content height → 0
    invalidBox: pan(900, 0),     // bad box height → 0
  };

  // CSS pan contract: a taller canvas inside an overflow:hidden host,
  // translated up by terminalPanY(content, box), must land its bottom edge
  // exactly at the host's bottom edge — that's how the agent's prompt row
  // stays visible just above the keyboard. Build a throwaway host/child so
  // the contract is pinned without a live xterm.
  const host = document.createElement('div');
  host.style.cssText =
    'position:fixed;left:0;top:0;width:200px;height:300px;overflow:hidden';
  const canvas = document.createElement('div');
  canvas.style.cssText = 'width:200px;height:500px';
  host.appendChild(canvas);
  document.body.appendChild(host);
  const shift = pan(500, 300);   // 200
  canvas.style.transform = 'translateY(-' + shift + 'px)';
  const hostRect = host.getBoundingClientRect();
  const canvasRect = canvas.getBoundingClientRect();
  const bottomGap = Math.round(canvasRect.bottom - hostRect.bottom);
  const topClipped = Math.round(hostRect.top - canvasRect.top);
  document.body.removeChild(host);

  return { ...helper, shift, bottomGap, topClipped };
}
"""


def test_fullscreen_keyboard_pan(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    r = authed_page.evaluate(_PROBE)

    assert r["overflow"] == 340, (
        f"a 900px canvas in a 560px box returned {r['overflow']!r}, expected "
        "340 — the canvas won't pan up far enough and the prompt stays hidden "
        "behind the keyboard"
    )
    assert r["exact"] == 0, (
        f"an exact-fit canvas returned {r['exact']!r}, expected 0 — it would "
        "pan a canvas that already fits, hiding its top row for nothing"
    )
    assert r["shorter"] == 0, (
        f"a canvas shorter than the box returned {r['shorter']!r}, expected 0 "
        "— a negative pan would shift the canvas down and open a gap below "
        "the prompt"
    )
    assert r["rounds"] == 361, (
        f"a fractional content height returned {r['rounds']!r}, expected 361 "
        "(rounded) — a sub-pixel transform leaves a hairline seam"
    )
    assert r["invalidContent"] == 0 and r["invalidBox"] == 0, (
        f"invalid inputs returned {r['invalidContent']!r}/{r['invalidBox']!r}, "
        "expected 0 — a bad measurement must not throw or pan blindly"
    )
    assert r["shift"] == 200, (
        f"pan(500,300) returned {r['shift']!r}, expected 200 (the CSS contract "
        "probe used the wrong shift)"
    )
    assert r["bottomGap"] == 0, (
        f"the panned canvas's bottom edge was {r['bottomGap']}px off the host "
        "bottom, expected 0 — the prompt row won't sit flush above the keyboard"
    )
    assert r["topClipped"] == 200, (
        f"the host clipped {r['topClipped']}px off the canvas top, expected 200 "
        "— overflow:hidden must hide the panned-off rows, not leak them over "
        "the page"
    )
