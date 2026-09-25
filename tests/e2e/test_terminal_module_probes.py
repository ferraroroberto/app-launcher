"""Page-load-then-evaluate probes of the terminal's client-side modules.

Every probe here needs nothing but a loaded SPA: it imports a terminal module
(``terminal.js``, ``terminal-connection.js``, ``terminal-touch.js``) or reads
the CSS tokens, and exercises a pure helper, a throwaway DOM node or a fake
WebSocket. None opens a PTY, mocks a route or uses a fixture beyond
``authed_page``. They used to be eight separate tests across seven modules,
each paying its own browser context and page boot for one ``evaluate``; #1215
folded them into one test on one page load.

Each probe is kept verbatim as a named helper whose docstring is its original
module's docstring, so the issue each one pins stays readable here:

* ``_probe_no_follow_switch_remains`` — #383 (``test_terminal_light_theme.py``)
* ``_probe_keyboard_overlay_height`` — #135 (``test_keyboard_overlay.py``)
* ``_probe_fullscreen_keyboard_pan`` — #264 (``test_fullscreen_keyboard_pan.py``)
* ``_probe_route_frame_classifies_shutdown_vs_output`` — #181
  (``test_shutdown_frame.py``; Chromium-only there, both projections now)
* ``_probe_reconnect_repaint_batch_discards_stale_state`` — the post-#435
  repaint-batch race (``test_repaint_batch_reconnect_race.py``)
* ``_probe_reconnect_closes_the_previous_socket`` — #832
  (``test_terminal_reconnect.py``)
* ``_probe_native_touch_scroll_routing`` — #23 (``test_terminal_native_scroll.py``)
* ``_probe_terminal_tokens_follow_app_theme`` — #383
  (``test_terminal_light_theme.py``)

Order matters only at the edges: the read-only DOM check runs first, the
probes that build throwaway nodes or swap ``window.WebSocket`` restore what
they touched, the native-scroll probe waits for the xterm global, and the
theme flip (which leaves ``html[data-theme]`` changed) runs last.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

# iPhone keyboard overlay, visualViewport and native touch-scroll probes.
pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# The SPA loads its modules cache-busted (`state.js?v=<asset_hash>`). A bare
# import('/static/state.js') would evaluate a SECOND module instance with its
# own empty state — silently testing a parallel universe. Resolve the page's
# real module URL from the resource timeline so the import shares the live
# instance (same pattern as test_warm_terminal_reopen.py).
_LIVE_MODULE = """
(name) => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/' + name + '?v='));
  return hit || ('/static/' + name);
}
"""


# ------------------------------------------------------------------ #383

def _probe_no_follow_switch_remains(page: Page) -> None:
    """The #359 opt-in switch is gone — following the theme is not a
    setting any more.

    Formerly ``test_terminal_light_theme.py::test_no_follow_switch_remains``.
    """
    assert page.locator("#termFollowTheme").count() == 0


# ------------------------------------------------------------------ #135

_KEYBOARD_OVERLAY_PROBE = r"""
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
    r = page.evaluate(_KEYBOARD_OVERLAY_PROBE)

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


# ------------------------------------------------------------------ #264

_FULLSCREEN_PAN_PROBE = r"""
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
    pure helper directly plus the CSS pan contract it depends on: inside an
    ``overflow:hidden`` host, translating a taller child up by the helper's
    value must drop the child's bottom edge to the host's bottom edge (so the
    prompt lands just above where the keyboard would be), and clamp at 0 so a
    canvas already shorter than the box never shifts down (which would expose a
    gap below the prompt).
    """
    r = page.evaluate(_FULLSCREEN_PAN_PROBE)

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


# ------------------------------------------------------------------ #181

# Exercise routeFrame in the page across the frame kinds that share the
# server→client stream. The only variables are the frame text and whether
# the page is a mirror window.
_ROUTE_FRAME_PROBE = r"""
async () => {
  const m = await import('/static/terminal.js');
  const r = (data, isMirror) => m.routeFrame(data, isMirror);
  return {
    shutdownMirror: r('{"type":"shutdown"}', true),
    shutdownPhone:  r('{"type":"shutdown"}', false),
    // Ordinary terminal output — including a program that prints a
    // brace-leading line or some *other* JSON shape — must always write.
    plainMirror:    r('hello world\r\n', true),
    plainPhone:     r('hello world\r\n', false),
    braceNonJson:   r('{ not valid json', true),
    otherJsonObj:   r('{"type":"resize","rows":40}', true),
    nonString:      r(null, true),
  };
}
"""


def _probe_route_frame_classifies_shutdown_vs_output(page: Page) -> None:
    """Regression pin for #181 — the cooperative WS-shutdown close fallback.

    Formerly ``test_shutdown_frame.py::test_route_frame_classifies_shutdown_vs_output``
    (Chromium-only there; it rides the WebKit projection too now that it
    shares this page load).

    The bug: the PC mirror window's "Stop & Close" had two documented close
    paths — a primary Win32 ``WM_CLOSE`` (issue #20) and a cooperative
    ``{"type":"shutdown"}`` WebSocket frame as a fallback — but the client half
    of the fallback was *dead code*. ``terminal.js`` ``ws.onmessage`` wrote every
    frame to xterm unconditionally, so when the Win32 path missed (no HWND ever
    captured) nothing closed the window and the shutdown JSON was printed into
    the terminal as garbage.

    Fix: ``ws.onmessage`` routes each frame through ``routeFrame`` first — a
    mirror window self-closes on a shutdown frame, the phone drops it, and
    ordinary terminal output still writes through. This pins that routing
    *decision* deterministically (no live PTY, so it runs on CI too, unlike the
    ``launched_pty_session``-gated ``test_edge_mirror_close.py``); the actual OS
    window close is the manual / e2e acceptance step, and the server half (the
    frame being sent) is pinned by ``tests/test_session_host_pty_stop.py``.
    """
    res = page.evaluate(_ROUTE_FRAME_PROBE)

    # A shutdown control frame closes a mirror window and is dropped on the
    # phone — never written to xterm in either case.
    assert res["shutdownMirror"] == "close-mirror"
    assert res["shutdownPhone"] == "swallow"

    # Everything else is ordinary terminal output and must be written, so no
    # real PTY byte is ever swallowed: plain text, a brace-leading non-JSON
    # line, an unrelated JSON object, and a non-string frame.
    assert res["plainMirror"] == "write"
    assert res["plainPhone"] == "write"
    assert res["braceNonJson"] == "write"
    assert res["otherJsonObj"] == "write"
    assert res["nonString"] == "write"


# ----------------------------------------------- post-#435 repaint race

_REPAINT_BATCH_PROBE = """
async () => {
  const live = """ + _LIVE_MODULE + """;
  const mod = await import(live('terminal-connection.js'));

  const terminal = {
    isFullscreen: true,
    term: { element: document.createElement('div') },
  };

  // Connection A: a batch starts (its ws.onopen), one message arrives and
  // arms the quiet timer (ws.onmessage's real batching branch) — then the
  // connection dies before that timer fires and before it ever flushes,
  // exactly as a dropped WebSocket would leave things.
  mod.beginRepaintBatch(terminal);
  terminal.batchBuf.push('STALE-FROM-DEAD-CONNECTION-A');
  terminal.batchQuietTimer = setTimeout(function () {}, 60000);
  const staleTimerHandle = terminal.batchQuietTimer;

  // Connection B: the reconnect succeeds and its own ws.onopen fires
  // beginRepaintBatch again — this must start a clean slate.
  mod.beginRepaintBatch(terminal);

  return {
    batchBufAfterSecondOpen: terminal.batchBuf.slice(),
    quietTimerWasReplaced: terminal.batchQuietTimer !== staleTimerHandle,
    quietTimerNowNull: terminal.batchQuietTimer === null,
  };
}
"""


def _probe_reconnect_repaint_batch_discards_stale_state(page: Page) -> None:
    """Regression pin: stale repaint-batch state across a fullscreen reconnect.

    Formerly
    ``test_repaint_batch_reconnect_race.py::test_reconnect_repaint_batch_discards_stale_state``.

    Reported after #435 shipped: during an *active* Codex conversation (not a
    cold resume — that path was already fixed and confirmed working), the phone
    sometimes showed the true beginning of the conversation, then a chunk of
    missing content, then the latest lines — a hole in the middle rather than a
    simple truncation.

    Root cause: WebSocket drops are routine on mobile (screen dim, a brief
    background, a network blip) and every drop that isn't a clean session-end
    schedules a silent reconnect (``terminal-connection.js`` ``scheduleReconnect``
    / ``ws.onclose``). Each successful reconnect's ``ws.onopen`` calls
    ``beginRepaintBatch(terminal)`` again to conceal the incoming snapshot. But
    ``beginRepaintBatch`` only reset ``batchTimer`` — it left a leftover
    ``batchQuietTimer`` (armed by the *previous, now-dead* connection's
    ``ws.onmessage``) still ticking, and never cleared a partially-filled
    ``batchBuf``. The dead connection's own snapshot (sent as two separate WS
    messages: ``_CLEAR_FRAME`` then the history+frame payload) could have been
    interrupted mid-delivery when the socket dropped, so ``batchBuf`` might hold
    only the clear-frame with none of the actual content. If that stale timer
    then fires, it flushes whatever happens to be sitting in the shared
    ``batchBuf`` at that moment — a snapshot from a dead connection, mixed with
    or clobbered by the new connection's own writes, landing anywhere from a
    partial paint to the exact "gap in the middle" symptom reported.

    This drives the real, exported ``beginRepaintBatch`` against a
    minimal fake terminal object (no live PTY / WebSocket needed — the bug is
    entirely a client-side state-machine issue) and proves a second call, made
    to simulate the next connection's ``onopen``, must never see stale content
    or a still-armed timer left over from the first.
    """
    r = page.evaluate(_REPAINT_BATCH_PROBE)

    assert r["batchBufAfterSecondOpen"] == [], (
        f"a fresh reconnect's beginRepaintBatch() still carried "
        f"{r['batchBufAfterSecondOpen']!r} from a dead prior connection — "
        "a stale quiet-timer flushing this later corrupts the paint with "
        "content from two different connections (the reported "
        "'beginning visible, middle missing' symptom)"
    )
    assert r["quietTimerNowNull"], (
        "the dead connection's quiet timer must be cancelled by the next "
        "beginRepaintBatch(), not left armed to fire later against "
        "whatever batchBuf happens to hold by then"
    )


# ------------------------------------------------------------------ #832

def _probe_reconnect_closes_the_previous_socket(page: Page) -> None:
    """Regression for #832: `connectTerminalWs` used to detach the previous
    socket's four handlers but never call `.close()` on it, so re-running
    connect while the old socket was still OPEN abandoned it as a live
    session-host subscriber for the rest of the page's life (reachable via
    the #610 "tap to reconnect" affordance firing while the WS was in fact
    healthy).

    Formerly ``test_terminal_reconnect.py::test_reconnect_closes_the_previous_socket``.

    Exercises the live module directly against a fully fake, deterministic
    WebSocket (same shape as `_SILENT_WS` in test_terminal_reconnect.py)
    instead of a real session-host socket — a real WS's OPEN→CLOSED timing
    isn't controllable enough to pin the exact "still OPEN when reconnect
    runs" scenario without a race. No PTY session needed:
    `terminal-connection.js` is part of the static import graph `main.js`
    already pulled in on boot."""
    live = _LIVE_MODULE
    result = page.evaluate(
        """
        async () => {
          const live = """ + live + """;
          const { connectTerminalWs } = await import(live('terminal-connection.js'));

          class FakeWs extends EventTarget {
            constructor(url) {
              super();
              this.url = url;
              this.readyState = 1; // OPEN immediately — deterministic
              this.onopen = null; this.onmessage = null;
              this.onerror = null; this.onclose = null;
              this.closeCalled = false;
            }
            send() {}
            close() {
              this.closeCalled = true;
              this.readyState = 3; // CLOSED
            }
          }
          FakeWs.CONNECTING = 0; FakeWs.OPEN = 1; FakeWs.CLOSING = 2; FakeWs.CLOSED = 3;
          const realWebSocket = window.WebSocket;
          window.WebSocket = FakeWs;
          try {
            const terminal = { sid: 'fake-sid', tt: 'fake-tt', ws: null };
            connectTerminalWs(terminal); // first connect
            const oldWs = terminal.ws;
            const oldWasOpen = oldWs.readyState === 1; // capture BEFORE reconnecting
            connectTerminalWs(terminal); // reconnect while oldWs is still OPEN
            return {
              oldWasOpen: oldWasOpen,
              closeCalledOnOld: oldWs.closeCalled,
              oldReadyStateAfter: oldWs.readyState,
            };
          } finally {
            window.WebSocket = realWebSocket;
          }
        }
        """
    )
    assert result["oldWasOpen"], "precondition: old socket must still be OPEN when reconnect runs"
    assert result["closeCalledOnOld"], (
        "connectTerminalWs must call .close() on the previous still-OPEN "
        "socket, not just detach its handlers, or it stays a live "
        "session-host subscriber forever (#832)"
    )
    assert result["oldReadyStateAfter"] != 1


# ------------------------------------------------------------------- #23

# Builds a standalone xterm, wires the real module, and runs the probes.
# WebKit rejects `new Touch()` / `new TouchEvent()`, so touches are
# plain Events with expando touch lists — the module only reads
# touches[0]/changedTouches[0].clientX/Y and timeStamp.
_NATIVE_SCROLL_PROBE = r"""
async () => {
  const { enableNativeTouchScroll } = await import('/static/terminal-touch.js');

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
    page.wait_for_function(
        "() => typeof window.Terminal === 'function'", timeout=5_000
    )
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


def test_terminal_module_contracts(authed_page: Page, base_url: str) -> None:
    page = authed_page
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    _probe_no_follow_switch_remains(page)
    _probe_keyboard_overlay_height(page)
    _probe_fullscreen_keyboard_pan(page)
    _probe_route_frame_classifies_shutdown_vs_output(page)
    _probe_reconnect_repaint_batch_discards_stale_state(page)
    _probe_reconnect_closes_the_previous_socket(page)
    _probe_native_touch_scroll_routing(page)
    _probe_terminal_tokens_follow_app_theme(page)
