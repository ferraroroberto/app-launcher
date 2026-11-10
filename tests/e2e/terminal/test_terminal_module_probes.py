"""Page-load-then-evaluate probes of the terminal's client-side modules.

Every probe here needs nothing but a loaded SPA: it imports a terminal module
(``terminal.js``, ``terminal-connection.js``, ``terminal-touch.js``) or reads
the CSS tokens, and exercises a pure helper, a throwaway DOM node or a fake
WebSocket. None opens a PTY or uses a fixture beyond ``authed_page``, and only
the git-refresh probe mocks a route. They used to be eight separate tests across seven modules,
each paying its own browser context and page boot for one ``evaluate``; #1215
folded them into one test on one page load.

Each probe is kept verbatim as a named helper whose docstring is its original
module's docstring, so the issue each one pins stays readable here:

* ``_probe_no_follow_switch_remains`` — #383 (``test_terminal_light_theme.py``)
* ``_probe_keyboard_overlay_height`` — #135 (``test_keyboard_overlay.py``)
* ``_probe_fullscreen_keyboard_pan`` — #264 (``test_fullscreen_keyboard_pan.py``)
* ``_probe_native_touch_scroll_routing`` — #23 (``test_terminal_native_scroll.py``)
* ``_probe_terminal_tokens_follow_app_theme`` — #383
  (``test_terminal_light_theme.py``)
* ``_probe_git_refresh_button_rechecks_without_a_popover`` — #1434
  (``test_status_popover.py``), the one probe that mocks a route: the Code
  tab's git-status fetch, served before the load so it counts requests
  instead of running git. Chromium only, as it always ran.

The probes that needed only a pure helper (``routeFrame``, the repaint-batch
reset, the reconnect closing its old socket, and the value tables of
``keyboardOverlayHeight`` / ``terminalPanY``) moved to
``tests/js/terminal_module_helpers.test.mjs``, which runs under plain Node;
the probes left here read layout, computed style or a real xterm.

Order matters only at the edges: the read-only DOM check runs first, then
the git-refresh probe (it opens the Projects card and taps its button), the
probes that build throwaway nodes remove what they added, the native-scroll probe waits for the xterm global, and the
theme flip (which leaves ``html[data-theme]`` changed) runs last.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import wait_until

# iPhone keyboard overlay, visualViewport and native touch-scroll probes.
pytestmark = [pytest.mark.smoke, pytest.mark.iphone]


# ------------------------------------------------------------------ #383

def _probe_no_follow_switch_remains(page: Page) -> None:
    """The #359 opt-in switch is gone — following the theme is not a
    setting any more.

    Formerly ``test_terminal_light_theme.py::test_no_follow_switch_remains``.
    """
    assert page.locator("#termFollowTheme").count() == 0


# ----------------------------------------------------------------- #1434

def _serve_git_status(page: Page) -> list[str]:
    """Fulfil /api/claude-code/git-status with an empty body, recording each URL."""
    calls: list[str] = []

    def _fulfill(route):
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"projects": []}))

    page.route("**/api/claude-code/git-status*", _fulfill)
    return calls


def _probe_git_refresh_button_rechecks_without_a_popover(page: Page, calls: list[str]) -> None:
    """Regression pin for #1434 (was #139's off-main status popover).

    Formerly ``test_status_popover.py::test_git_refresh_button_rechecks_without_a_popover``.

    #139 put an off-main popover behind the Code tab's "Git status" button.
    #1434 removed both: the rows themselves are the off-main list ("on
    <branch>" in their meta), and the refresh is an icon button in the Projects
    card's toolbar whose only feedback is the footer's "Git checked just now".
    This pins that the refresh re-runs the git check, that no popover opens,
    and that the old button is gone.
    """
    wait_until(page, lambda: len(calls) >= 1, "the boot git-status fetch")

    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    refresh = page.locator(".projects-toolbar #gitRefreshBtn")
    expect(refresh).to_be_visible()
    expect(refresh).to_have_attribute("aria-label", "Refresh git status")
    expect(page.locator("#gitStatusBtn")).to_have_count(0)
    expect(page.locator("#gitStatusSummary")).to_have_count(0)

    before = len(calls)
    refresh.click()
    wait_until(page, lambda: len(calls) > before, "the refresh's git-status fetch")
    assert calls[-1].endswith("?fresh=true"), "the refresh button must ask for a scan that starts now"
    expect(page.locator("#gitCheckedFooter")).to_have_text("Git checked just now")
    expect(refresh).to_be_enabled()


# ------------------------------------------------------------------ #135

_KEYBOARD_OVERLAY_PROBE = r"""
async () => {
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
    constrained, offsetTop, released, releasedTop,
    innerH: window.innerHeight,
  };
}
"""


def _probe_keyboard_overlay_height(page: Page) -> None:
    """Regression pin for issue #135 — keep the prompt above the keyboard.

    Formerly ``test_keyboard_overlay.py::test_keyboard_overlay_height``.

    On iOS the software keyboard shrinks ``window.visualViewport.height`` but
    NOT the layout viewport, so the ``position:fixed; inset:0`` terminal
    overlay keeps covering the whole screen *behind* the keyboard and the
    active prompt row renders hidden under it. ``keyboardOverlayHeight()`` in
    ``terminal.js`` decides when to pin the overlay to the visual-viewport
    height (keyboard up) versus release it to the CSS ``100dvh`` full height
    (keyboard down / minor chrome changes); ``applySize()`` then re-fits
    xterm to the smaller box.

    The pure helper's values (equal heights, a chrome-sized shrink, a keyboard
    shrink, invalid input, rounding) are pinned without a browser in
    ``tests/js/terminal_module_helpers.test.mjs``. What stays here needs real
    layout: the CSS over-constraint contract the pin depends on (the keyboard
    can't be raised in a headless browser, but the box can be sized):

    * applying the height + ``bottom:auto`` shrinks the overlay, and clearing
      both restores it to the full viewport height;
    * applying ``top`` moves the overlay down to track ``visualViewport.offsetTop``
      (issue #135 reopen — iOS shifts the visual viewport down to sweep the
      focused line into view; without matching that offset the overlay slides off
      the top and a band of the page shows through above the keyboard), and
      clearing ``top`` returns it to the ``inset:0`` origin.
    """
    r = page.evaluate(_KEYBOARD_OVERLAY_PROBE)

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


# ------------------------------------------------------------------ #264

_FULLSCREEN_PAN_PROBE = r"""
async () => {
  const pan = (await import('/static/terminal.js')).terminalPanY;

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

  return { shift, bottomGap, topClipped };
}
"""


def _probe_fullscreen_keyboard_pan(page: Page) -> None:
    """Regression pin for issue #264 — pan, don't reflow, a full-screen TUI.

    Formerly ``test_fullscreen_keyboard_pan.py::test_fullscreen_keyboard_pan``.

    On iPhone the software keyboard shrinks ``visualViewport.height``. For an
    inline agent (Claude) ``applySize()`` re-fits xterm to the smaller box
    (issue #135). For a full-screen *differential* agent (Codex/ratatui) that
    reflow is harmful: it changes the PTY row count, which SIGWINCHes the agent
    into repainting its whole frame on every keyboard open/close — the visible
    "refreshment" the user reported. The fix keeps the PTY at its stable size
    and instead *pans* the fixed canvas up so the bottom row (the prompt) stays
    above the keyboard. ``terminalPanY()`` in ``terminal.js`` computes that shift.

    The keyboard can't be raised in a headless browser, so this exercises the
    CSS pan contract it depends on: inside an
    ``overflow:hidden`` host, translating a taller child up by the helper's
    value must drop the child's bottom edge to the host's bottom edge (so the
    prompt lands just above where the keyboard would be), and clamp at 0 so a
    canvas already shorter than the box never shifts down (which would expose a
    gap below the prompt).
    """
    r = page.evaluate(_FULLSCREEN_PAN_PROBE)

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


# ------------------------------------------------------------------ #1937

# Runs the real `loadWebgl` against a throwaway terminal. A vendored addon
# that does not match the vendored core throws inside `term.loadAddon`, which
# `loadWebgl` swallows into the DOM renderer, so the only visible symptom is
# a null return — pin that where a webgl2 context exists.
_WEBGL_RENDERER_PROBE = r"""
async () => {
  const { loadWebgl } = await import('/static/terminal.js');
  await (await import('/static/terminal/xterm-loader.js')).loadXterm();
  const hasGl2 = !!document.createElement('canvas').getContext('webgl2');

  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:0;top:0;width:320px;height:240px;';
  document.body.appendChild(host);
  const term = new window.Terminal({ scrollback: 100, fontSize: 13 });
  term.open(host);
  const canvasesBefore = host.querySelectorAll('.xterm-screen canvas').length;
  const addon = loadWebgl(term);
  const canvasesAfter = host.querySelectorAll('.xterm-screen canvas').length;
  const loaded = !!addon;
  if (addon) addon.dispose();
  term.dispose();
  host.remove();
  return { hasGl2, loaded, canvasesBefore, canvasesAfter };
}
"""


def _probe_webgl_renderer_loads(page: Page) -> None:
    """The WebGL addon loads on the vendored core wherever WebGL2 exists.

    Where the engine has no webgl2 context the DOM fallback is the contract,
    so there is nothing to pin.
    """
    r = page.evaluate(_WEBGL_RENDERER_PROBE)
    if not r["hasGl2"]:
        return
    assert r["loaded"], (
        "loadWebgl() fell back to the DOM renderer although webgl2 is "
        "available: the vendored addon-webgl.js does not match xterm.js"
    )
    assert r["canvasesAfter"] > r["canvasesBefore"], r


# ------------------------------------------------------------------- #23

# Builds a standalone xterm, wires the real module, and runs the probes.
# WebKit rejects `new Touch()` / `new TouchEvent()`, so touches are
# plain Events with expando touch lists — the module only reads
# touches[0]/changedTouches[0].clientX/Y and timeStamp.
_NATIVE_SCROLL_PROBE = r"""
async () => {
  const { enableNativeTouchScroll } = await import('/static/terminal/terminal-touch.js');
  await (await import('/static/terminal/xterm-loader.js')).loadXterm();

  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:0;top:0;width:320px;height:240px;';
  document.body.appendChild(host);
  const term = new window.Terminal({ scrollback: 1000, fontSize: 13 });
  term.open(host);

  let focusCount = 0;
  const realFocus = term.focus.bind(term);
  term.focus = () => { focusCount++; realFocus(); };

  const screen = host.querySelector('.xterm-screen');
  const viewport = host.querySelector('.xterm-viewport');

  const dispose = enableNativeTouchScroll(term);
  const screenPE_wired = getComputedStyle(screen).pointerEvents;

  // A touch dispatched on a child must be swallowed before it reaches
  // the document (capture-phase stopPropagation on the xterm root).
  let docSawTouch = 0;
  const docListener = () => { docSawTouch++; };
  document.addEventListener('touchmove', docListener);

  const fire = (el, type, x, y) => {
    const pt = { identifier: 1, clientX: x, clientY: y, pageX: x, pageY: y };
    const live = type === 'touchend' ? [] : [pt];
    const ev = new Event(type, { bubbles: true, cancelable: true });
    ev.touches = live; ev.targetTouches = live; ev.changedTouches = [pt];
    el.dispatchEvent(ev);
  };

  fire(viewport, 'touchmove', 100, 100);
  const swallowedWhileWired = docSawTouch === 0;

  // Stationary tap → focus; a travelled touch → no focus.
  const focusBeforeTap = focusCount;
  fire(viewport, 'touchstart', 100, 100);
  fire(viewport, 'touchend', 101, 101);
  const tapFocused = focusCount > focusBeforeTap;

  const focusBeforeDrag = focusCount;
  fire(viewport, 'touchstart', 100, 100);
  fire(viewport, 'touchend', 100, 200);
  const dragFocused = focusCount > focusBeforeDrag;

  // After dispose: screen restored, touches no longer swallowed.
  dispose();
  const screenPE_disposed = getComputedStyle(screen).pointerEvents;
  docSawTouch = 0;
  fire(viewport, 'touchmove', 100, 100);
  const swallowedAfterDispose = docSawTouch === 0;

  document.removeEventListener('touchmove', docListener);
  host.remove();
  return {
    screenPE_wired, swallowedWhileWired, tapFocused, dragFocused,
    screenPE_disposed, swallowedAfterDispose,
  };
}
"""


def _probe_native_touch_scroll_routing(page: Page) -> None:
    """Regression pin for issue #23 — native touch-momentum scrolling.

    Formerly ``test_terminal_native_scroll.py::test_native_touch_scroll_routing``.

    The phone terminal relies on iOS native inertial scrolling of xterm's
    `.xterm-viewport`. ``enableNativeTouchScroll()`` in
    ``terminal-touch.js`` makes that reachable: it sets `.xterm-screen`
    (the text layer that otherwise intercepts touches) to
    `pointer-events:none`, and swallows xterm's own root-level touch
    events in the capture phase so xterm can't `preventDefault` and cancel
    the native momentum.

    This exercises that module directly on a standalone xterm — the real
    integration path can't be reached here because the e2e harness
    connects over loopback, which the app treats as the PC mirror (where
    the feature is intentionally skipped). It pins:

    * `.xterm-screen` becomes `pointer-events:none`.
    * A touch event on a child is swallowed in the capture phase — it
      never reaches a document-level listener (so xterm's bubble-phase
      handler never runs either).
    * A stationary tap re-focuses the terminal; a travelled touch does not.
    * `dispose()` restores `.xterm-screen` and stops swallowing.
    """
    r = page.evaluate(_NATIVE_SCROLL_PROBE)

    assert r["screenPE_wired"] == "none", (
        f".xterm-screen pointer-events is {r['screenPE_wired']!r} after wiring, "
        "expected 'none' — touches won't fall through to the native viewport"
    )
    assert r["swallowedWhileWired"], (
        "a touchmove on a child reached document — xterm's root touch handler "
        "is no longer swallowed, so it can preventDefault and kill native momentum"
    )
    assert r["tapFocused"], "a stationary tap did not re-focus the terminal"
    assert not r["dragFocused"], "a travelled touch (a scroll) wrongly re-focused"
    assert r["screenPE_disposed"] != "none", (
        f"dispose() left .xterm-screen pointer-events at {r['screenPE_disposed']!r}"
    )
    assert not r["swallowedAfterDispose"], (
        "touches still swallowed after dispose() — listeners were not removed"
    )


# ------------------------------------------------------------------ #383

def _term_bg(page: Page) -> str:
    return page.evaluate(
        "getComputedStyle(document.documentElement)"
        ".getPropertyValue('--term-bg').trim()"
    )


def _probe_terminal_tokens_follow_app_theme(page: Page) -> None:
    """Terminal always follows the app theme (issue #383; supersedes #359):
    the terminal screen tokens follow ``html[data-theme]`` directly — light
    app theme → light ``--term-bg``/``--term-fg``, dark app theme → the dark
    screen. No opt-in switch exists.

    Formerly ``test_terminal_light_theme.py::test_terminal_tokens_follow_app_theme``.
    Leaves ``html[data-theme]`` flipped, so it runs last.
    """
    page.evaluate("document.documentElement.dataset.theme = 'light'")
    assert _term_bg(page) == "#ffffff"
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert _term_bg(page) == "#0a0a0a"


def test_terminal_module_contracts(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    page = authed_page
    git_calls = _serve_git_status(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    _probe_no_follow_switch_remains(page)
    if browser_name == "chromium":
        _probe_git_refresh_button_rechecks_without_a_popover(page, git_calls)
    _probe_keyboard_overlay_height(page)
    _probe_fullscreen_keyboard_pan(page)
    _probe_native_touch_scroll_routing(page)
    _probe_webgl_renderer_loads(page)
    _probe_terminal_tokens_follow_app_theme(page)
