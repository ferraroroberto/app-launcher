"""The terminal re-fits when the composer under it changes height (#2660).

A growing draft shrinks the terminal's box with no viewport event, so xterm
kept its old row count and the prompt line sat clipped under the composer.
Pinned at phone and desktop widths: the last row stays inside the box, the box
ends above the composer, and the rows follow the box both ways.

The Chat pane has its own composer, so a long Chat draft must leave the
terminal alone; that is the first step below.

A leg of ``test_compose_bar.py``'s auto-grow test, which shares its setup.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

_VIEWPORTS = [(390, 844), (1280, 900)]
_DRAFT = "\n".join(f"line {i} of a long dictated message" for i in range(12))

# The page's own module instance (its URL is cache-busted).
_LIVE_TERMINAL = """
async () => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/state.js?v='));
  const { state } = await import(hit || '/static/state.js');
  window.__term = () => state.terminal;
}
"""

# Enough scrollback that "the last row is in view" means something.
_FILL = """
() => new Promise((done) => window.__term().term.write(
  Array.from({ length: 120 }, (_, i) => 'row ' + i).join('\\r\\n') + '\\r\\nPROMPT>', done))
"""

_MEASURE = """
() => {
  const box = (s) => document.querySelector(s).getBoundingClientRect();
  const term = window.__term().term;
  const screen = box('#terminalHost .xterm-screen');
  const host = box('#terminalHost');
  const buf = term.buffer.active;
  return {
    rows: term.rows,
    clipped: screen.bottom - host.bottom,
    spare: host.bottom - screen.bottom,
    rowHeight: screen.height / term.rows,
    overlap: host.bottom - box('#terminalComposeBar').top,
    atTail: buf.viewportY >= buf.baseY,
  };
}
"""

# Nothing clipped or overlapped, at the tail, and no spare room for a row.
_FITTED = f"""
() => {{
  const m = ({_MEASURE})();
  return m.clipped <= 0.5 && m.overlap <= 0.5 && m.atTail && m.spare < m.rowHeight + 5;
}}
"""


def _fitted_rows(page: Page, step: str) -> int:
    try:
        page.wait_for_function(_FITTED, timeout=5_000)
    except PlaywrightTimeoutError:
        raise AssertionError(f"{step}: terminal not fitted: {page.evaluate(_MEASURE)}")
    return page.evaluate("() => window.__term().term.rows")


def _show(page: Page, mode: str) -> None:
    page.locator("#sessionModeChat" if mode == "chat" else "#sessionModeTerminal").click()
    expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", mode)


def leg_terminal_refits_around_the_composer(page: Page, base_url: str, sid: str) -> None:
    """Open ``sid`` as a phone does (not as the PC mirror, which never re-fits),
    sweep both widths, then restore the viewport. Needs an ``unbundled`` page."""
    default_viewport = dict(page.viewport_size)
    page.set_viewport_size({"width": _VIEWPORTS[0][0], "height": _VIEWPORTS[0][1]})
    page.goto(f"{base_url}/?session={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    _show(page, "terminal")
    page.wait_for_selector("#terminalHost .xterm-screen", timeout=OVERLAY_OPEN_MS)
    page.evaluate(_LIVE_TERMINAL)
    page.wait_for_function(
        "() => { const t = window.__term(); return !!(t && t.ws && t.ws.readyState === 1"
        " && t.lastSentSize); }",
        timeout=OVERLAY_OPEN_MS,
    )
    chat_draft = page.locator("#chatComposeBar textarea")
    draft = page.locator("#terminalComposeBar textarea")

    for width, height in _VIEWPORTS:
        at = f"{width}x{height}"
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate(_FILL)
        full = _fitted_rows(page, f"{at} at rest")

        _show(page, "chat")
        chat_draft.fill(_DRAFT)
        _show(page, "terminal")
        assert _fitted_rows(page, f"{at} after a long Chat draft") == full
        _show(page, "chat")
        chat_draft.fill("")
        _show(page, "terminal")

        draft.fill(_DRAFT)
        assert _fitted_rows(page, f"{at} composer grown") < full
        _show(page, "chat")
        _show(page, "terminal")
        assert _fitted_rows(page, f"{at} grown, after a Chat round trip") < full

        draft.fill("")
        assert _fitted_rows(page, f"{at} draft cleared") == full

        draft.fill(_DRAFT)
        assert _fitted_rows(page, f"{at} composer grown again") < full
        page.locator("#terminalComposeBar .composer-send").click()
        expect(draft).to_have_value("")
        assert _fitted_rows(page, f"{at} draft sent") == full

    # A full-screen agent keeps its rows (every PTY resize replays its whole
    # transcript) and pans the canvas up instead.
    page.evaluate("() => { window.__term().isFullscreen = true; }")
    rows = page.evaluate("() => window.__term().term.rows")
    draft.fill(_DRAFT)
    page.wait_for_function(f"() => ({_MEASURE})().clipped <= 0.5", timeout=5_000)
    assert page.evaluate("() => window.__term().term.rows") == rows
    draft.fill("")
    page.wait_for_function(
        "() => !window.__term().term.element.style.transform", timeout=5_000
    )
    page.set_viewport_size(default_viewport)
