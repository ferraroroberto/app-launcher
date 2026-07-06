/* Live PTY terminal overlay: xterm.js + WebSocket + image paste/drop.
 *
 * Two flavours:
 *   phone (default)   — drives the PTY size; fit addon, resize frames.
 *   pc (loopback)     — mirror window opened by ?terminal=<sid>;
 *                       reads the phone's cols/rows from /api/sessions
 *                       and never resizes the PTY itself.
 *
 * The body is `position:fixed`-pinned while the overlay is open so iOS
 * rubber-band doesn't drag the page under the status bar.
 *
 * This module owns the PTY WebSocket lifecycle + the xterm instance
 * (connect/reconnect, sizing/keyboard-pan, the on-screen keys D-pad, and
 * image paste/drop). Three related concerns split out (issue #315):
 * the compose bar + dictation + OCR (terminal-compose.js), read-aloud UI
 * (terminal-readaloud.js, atop the terminal-readback.js engine), and the
 * PC-mirror-window title/guard logic (terminal-mirror.js).
 */

import { els, state, SESSIONS_POLL_MS } from './state.js';
import { apiFailToast, apiRaw, jsonApi, readToken, toast } from './api.js';
import { bindOutsideClickToClose } from './dom-utils.js';
import { fetchSessions, sessionTitle, stopSession } from './sessions.js';
import { enableNativeTouchScroll } from './terminal-touch.js';
import {
  announceMirrorWindow,
  isMirrorWindowSession,
  mirrorDocTitle,
  refreshTerminalTitle,
} from './terminal-mirror.js';
import {
  framePaste,
  resetComposeBar,
  sendSubmit,
  wireCompose,
  growComposeInput,
} from './terminal-compose.js';
import { closeSpeakPopover, revealReadAloudButton, stopReading, wireReadAloud } from './terminal-readaloud.js';
import {
  clearTerminalToken,
  ensureTerminalToken,
  readTerminalToken,
} from './webauthn.js';

// Test seam (#20/#135/#166/#181/#264): several pure helpers below are
// imported directly by the e2e suite via `import('/static/terminal.js')`
// (terminalPanY, keyboardOverlayHeight, sendSubmit, framePaste, routeFrame,
// mirrorDocTitle). Re-export the ones that moved to a split module so those
// imports keep working unchanged.
export { mirrorDocTitle, framePaste, sendSubmit };

function termWsUrl(sid, tt) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const params = new URLSearchParams();
  const bt = readToken();
  if (bt) params.set('token', bt);
  if (tt) params.set('tt', tt);
  const q = params.toString();
  return proto + '//' + location.host + '/api/claude-code/sessions/' +
    encodeURIComponent(sid) + '/ws' + (q ? '?' + q : '');
}

// Estimate the phone's terminal size (rows × cols) BEFORE a session
// exists, so the launch request can spawn the PTY at the right width and
// a full-screen differential TUI (Codex's ratatui) paints its first frame
// at the correct width instead of the legacy 40×120 — which wrapped/cut on
// a portrait phone (issue #126). Measures one monospace cell with the same
// font the live terminal uses, then divides the visual viewport. Cols (the
// cause of the "cut") is what matters; rows a touch high is harmless —
// applySize sends the exact size on WS open and ratatui reflows. Any
// failure falls back to the legacy 40×120 default.
const _TERM_FONT =
  '13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

export function estimateTermSize() {
  try {
    const span = document.createElement('span');
    span.style.cssText =
      'position:absolute;visibility:hidden;white-space:pre;font:' + _TERM_FONT;
    span.textContent = 'W'.repeat(100);
    document.body.appendChild(span);
    const rect = span.getBoundingClientRect();
    const cellW = rect.width / 100;
    const cellH = rect.height;
    document.body.removeChild(span);
    const vp = window.visualViewport;
    const w = (vp && vp.width) || window.innerWidth || 0;
    const h = (vp && vp.height) || window.innerHeight || 0;
    if (!(cellW > 0) || !(cellH > 0) || !(w > 0) || !(h > 0)) {
      return { rows: 40, cols: 120 };
    }
    return {
      rows: Math.max(10, Math.min(200, Math.floor(h / cellH))),
      cols: Math.max(20, Math.min(300, Math.floor(w / cellW))),
    };
  } catch (_) {
    return { rows: 40, cols: 120 };
  }
}

// Given the layout-viewport height and the current visual-viewport
// height, return the pixel height to pin the terminal overlay to so its
// bottom edge sits at the top of the on-screen keyboard — or null to
// release the override and let the overlay fill the screen via the CSS
// (100dvh). iOS shrinks `visualViewport.height` when the software
// keyboard slides up but does NOT shrink the layout viewport, so a
// `position:fixed; inset:0` overlay keeps covering the whole screen
// *behind* the keyboard and the active prompt row renders hidden under
// it (issue #135). Only a substantial shrink counts as the keyboard;
// smaller URL-bar / home-indicator chrome changes (<~120px) are left to
// the existing 100dvh + fit() path so this doesn't fight that behaviour.
const _KEYBOARD_SHRINK_PX = 120;

export function keyboardOverlayHeight(layoutHeight, visualHeight) {
  if (!(layoutHeight > 0) || !(visualHeight > 0)) return null;
  if (layoutHeight - visualHeight > _KEYBOARD_SHRINK_PX) {
    return Math.round(visualHeight);
  }
  return null;
}

// Pixels to shift a full-screen TUI's canvas *up* so its bottom row (the
// agent's prompt/composer) sits just above the on-screen keyboard, given
// the rendered content height and the visible box height. For a fullscreen
// differential agent (Codex/ratatui) the phone must NOT reflow xterm to the
// smaller keyboard box — reflowing changes the PTY rows, which SIGWINCHes
// the agent into repainting its whole frame on every keyboard open/close
// (the visible "refreshment", issue #264). Instead we keep the PTY at its
// stable size and pan the fixed canvas: translate it up by the overflow so
// the bottom stays visible while the top scrolls off behind the chrome.
// Clamped at 0 so a canvas already shorter than the box never shifts down.
export function terminalPanY(contentHeight, visibleHeight) {
  if (!(contentHeight > 0) || !(visibleHeight > 0)) return 0;
  return Math.max(0, Math.round(contentHeight - visibleHeight));
}

function setTerminalStatus(msg) {
  if (!els.terminalStatus) return;
  if (msg) {
    els.terminalStatus.textContent = msg;
    els.terminalStatus.hidden = false;
  } else {
    els.terminalStatus.hidden = true;
  }
}

// Reconnect backoff: 1s, 2s, 4s, then 8s forever (capped). After
// ~30s of failed attempts we stop retrying and swap the status line
// into a tappable "Tap to reconnect" affordance.
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000];
const RECONNECT_GIVE_UP_MS = 30000;

// A server→client WS frame is normally raw terminal output, but the
// session-host also multiplexes a cooperative {"type":"shutdown"} control
// frame (issue #20 close fallback / #181). Detect it cheaply — only
// JSON.parse frames that start with '{' so terminal throughput isn't taxed,
// and a program printing a brace-leading non-JSON line (or any other JSON
// shape) falls through to be rendered normally.
function isShutdownFrame(data) {
  if (typeof data !== 'string' || data.charCodeAt(0) !== 0x7b /* '{' */) {
    return false;
  }
  try {
    const msg = JSON.parse(data);
    return !!msg && typeof msg === 'object' && msg.type === 'shutdown';
  } catch (_) {
    return false;
  }
}

// Classify one server→client WS frame. Returns:
//   'close-mirror' — shutdown frame in a mirror window → caller window.close()s
//   'swallow'      — shutdown frame on the phone → drop, never render
//   'write'        — ordinary terminal output → caller writes it to xterm
// Pure + side-effect-free so the routing decision is unit-pinnable without a
// live socket (mirrors framePaste; see tests/e2e/test_shutdown_frame.py).
export function routeFrame(data, isMirror) {
  if (isShutdownFrame(data)) return isMirror ? 'close-mirror' : 'swallow';
  return 'write';
}

function connectWs(t) {
  // Re-bind ws.onclose for the previous socket so a late close event
  // from the dying connection doesn't interfere with the new one.
  if (t.ws) {
    try { t.ws.onopen = null; t.ws.onmessage = null;
      t.ws.onerror = null; t.ws.onclose = null; } catch (_) {}
  }
  const ws = new WebSocket(termWsUrl(t.sid, t.tt));
  t.ws = ws;

  ws.onopen = function () {
    if (t !== state.terminal) return;
    t.retryCount = 0;
    t.giveUpAt = 0;
    clearReconnect(t);
    setTerminalStatus(null);
    // Full-screen TUI (re)connect: drop any stale buffer so the server's
    // clean-frame repaint lands on an empty screen instead of crawling
    // through the previous frame's history (#270 tail-jump). The xterm
    // instance is reused across reconnects, so the old frame is still here.
    // Inline agents (Claude) keep their replayed scrollback — untouched.
    if (t.isFullscreen && t.term) { try { t.term.clear(); } catch (_) {} }
    if (t.applySize) t.applySize();
    if (t.term) t.term.focus();
  };
  ws.onmessage = function (ev) {
    // The session-host multiplexes a cooperative {"type":"shutdown"} control
    // frame onto the same stream as raw terminal output (issue #20 close
    // fallback / #181). It must never reach xterm as junk text. On a mirror
    // window it is the reliable self-close path for "Stop & Close" when the
    // Win32 WM_CLOSE never captured an HWND; on the phone it's simply dropped
    // (the server closes the socket with 4000 right after, which onclose
    // surfaces as "Session ended.").
    const route = routeFrame(ev.data, t.mirror);
    if (route === 'close-mirror') {
      closeTerminal();
      try { window.close(); } catch (_) { /* may be blocked; teardown stands */ }
      return;
    }
    if (route === 'swallow') return;
    if (!t.term) return;
    // tail -f follow: snap back to bottom on new output, but only
    // if the user was already there. If they scrolled up to read
    // history, leave them alone — they'll resume auto-follow by
    // scrolling back to the bottom themselves. The -1 fudge handles
    // iOS fractional touch-scroll states that would otherwise stick
    // the view one row above the tail forever.
    const b = t.term.buffer.active;
    const wasAtBottom = b.viewportY >= b.baseY - 1;
    t.term.write(ev.data, function () {
      if (wasAtBottom) {
        try { t.term.scrollToBottom(); } catch (_) {}
      }
    });
  };
  ws.onerror = function () { /* onclose drives UI */ };
  ws.onclose = function (ev) {
    if (t !== state.terminal) return;
    const reason = (ev && ev.reason) ? ev.reason : '';

    // Final, non-retriable close codes from the session-host.
    if (ev.code === 4000) { setTerminalStatus('Session ended.'); return; }
    if (ev.code === 4403) {
      setTerminalStatus('🔒 ' + (reason || 'Terminal is Tailscale-only') +
        ' — open the launcher over your Tailscale URL.');
      return;
    }
    if (ev.code === 4404) {
      setTerminalStatus('Session not found — it may have ended.');
      return;
    }

    // Passkey rejected: clear the cached terminal token and route
    // through the tap-to-reconnect affordance so the next attempt
    // re-prompts via ensureTerminalToken().
    if (ev.code === 4401) {
      clearTerminalToken();
      t.tt = '';
      setTapToReconnect(t, '🔒 ' + (reason || 'Passkey unlock required'));
      return;
    }

    // Everything else (1000/1001/1006, uvicorn ping timeout, 4502, …)
    // is the iOS-suspend case in practice — retry with backoff.
    if (!t.giveUpAt) t.giveUpAt = Date.now() + RECONNECT_GIVE_UP_MS;
    scheduleReconnect(t);
  };
}

function scheduleReconnect(t) {
  if (!t || t !== state.terminal) return;
  if (t.retryTimer) return;

  if (Date.now() >= t.giveUpAt) {
    setTapToReconnect(t, 'Tap to reconnect');
    return;
  }

  // iOS suspends background pages aggressively. Don't burn retries
  // while hidden — wait for the page to come back to the foreground
  // and try once at that moment, then resume the normal backoff.
  if (document.visibilityState !== 'visible') {
    setTerminalStatus('Reconnecting when visible…');
    if (!t.visibilityListener) {
      t.visibilityListener = function () {
        if (document.visibilityState === 'visible') {
          document.removeEventListener('visibilitychange', t.visibilityListener);
          t.visibilityListener = null;
          // Reset deadline and counter on wake so the user gets a
          // fresh 30s window the first time they look at the phone.
          t.retryCount = 0;
          t.giveUpAt = Date.now() + RECONNECT_GIVE_UP_MS;
          scheduleReconnect(t);
        }
      };
      document.addEventListener('visibilitychange', t.visibilityListener);
    }
    return;
  }

  const idx = Math.min(t.retryCount || 0, RECONNECT_DELAYS_MS.length - 1);
  const delay = RECONNECT_DELAYS_MS[idx];
  t.retryCount = (t.retryCount || 0) + 1;
  setTerminalStatus('Reconnecting…');
  t.retryTimer = setTimeout(function () {
    t.retryTimer = null;
    if (t !== state.terminal) return;
    connectWs(t);
  }, delay);
}

function setTapToReconnect(t, label) {
  if (!t || t !== state.terminal || !els.terminalStatus) return;
  clearReconnect(t);
  setTerminalStatus(label || 'Tap to reconnect');
  els.terminalStatus.style.cursor = 'pointer';
  els.terminalStatus.style.textDecoration = 'underline';
  t.tapHandler = function () {
    if (t !== state.terminal) return;
    els.terminalStatus.removeEventListener('click', t.tapHandler);
    t.tapHandler = null;
    els.terminalStatus.style.cursor = '';
    els.terminalStatus.style.textDecoration = '';
    t.retryCount = 0;
    t.giveUpAt = Date.now() + RECONNECT_GIVE_UP_MS;
    setTerminalStatus('Connecting…');
    // Refresh the terminal token if we lost it (4401 path); otherwise
    // ensureTerminalToken returns the cached value without prompting.
    ensureTerminalToken().then(function (tt) {
      if (t !== state.terminal) return;
      t.tt = tt;
      connectWs(t);
    }).catch(function (exc) {
      apiFailToast('Passkey unlock failed', exc);
      setTapToReconnect(t, 'Tap to reconnect');
    });
  };
  els.terminalStatus.addEventListener('click', t.tapHandler);
}

function clearReconnect(t) {
  if (!t) return;
  if (t.retryTimer) { clearTimeout(t.retryTimer); t.retryTimer = null; }
  if (t.visibilityListener) {
    document.removeEventListener('visibilitychange', t.visibilityListener);
    t.visibilityListener = null;
  }
  if (t.tapHandler && els.terminalStatus) {
    els.terminalStatus.removeEventListener('click', t.tapHandler);
    t.tapHandler = null;
    els.terminalStatus.style.cursor = '';
    els.terminalStatus.style.textDecoration = '';
  }
}

export async function openTerminal(session) {
  const sid = session.session_id;
  if (!sid) return;

  // The live terminal is Tailscale-only. If this connection can't reach
  // it (public Cloudflare tunnel, off-tailnet Wi-Fi), explain that up
  // front instead of opening a terminal that only says "Disconnected".
  if (state.status && state.status.terminal &&
      state.status.terminal.reachable === false) {
    closeTerminal();
    els.terminalOverlay.hidden = false;
    document.body.classList.add('terminal-open');
    lockBodyScroll();
    els.terminalTitle.textContent = sessionTitle(session);
    els.terminalHost.innerHTML = '';
    setTerminalStatus(
      '🔒 ' + (state.status.terminal.reason ||
        'The live terminal is Tailscale-only.')
    );
    return;
  }

  let tt = '';
  try {
    tt = await ensureTerminalToken();
  } catch (exc) {
    apiFailToast('Passkey unlock failed', exc);
    return;
  }
  closeTerminal();
  els.terminalOverlay.hidden = false;
  document.body.classList.add('terminal-open');
  lockBodyScroll();
  // Use the same stripping sessionTitle() applies elsewhere so Claude's
  // leading ✻/☁️/emoji prefix doesn't show up on first paint — the
  // agent icon next to the title is the redundancy.
  els.terminalTitle.textContent = sessionTitle(session);
  setTerminalStatus('Connecting…');

  // The PC mirror window is the launcher-spawned Edge --app window (issue
  // #241 — see terminal-mirror.js for why this can't just be the loopback
  // reason check). It renders whatever size the phone set and never resizes
  // the PTY — the phone is the single size authority, so the two clients
  // never fight (the server also ignores resize frames from role=pc).
  const isMirror = isMirrorWindowSession();

  // The compose bar (issue #37) is phone-only — the PC mirror already
  // has a real keyboard with full predictive support. Reset the button
  // visible on every (non-mirror) open so a prior mirror open can't
  // leave it stuck hidden.
  els.terminalCompose.hidden = isMirror;

  // The 🎤 dictation button (issue #165) needs the voice-transcriber
  // configured *and* MediaRecorder support; hide it otherwise so the
  // compose bar degrades to type-only. (It lives inside the compose bar,
  // so the PC mirror — where the bar never opens — already won't show it.)
  const voiceOn = !!(state.status && state.status.voice_dictation) &&
    !!window.MediaRecorder;
  els.terminalRecord.hidden = !voiceOn;

  // The 📷 screenshot-OCR button (issue #171) needs photo-ocr configured;
  // hide it otherwise. A plain file input, so no capability check beyond
  // the server flag. Pixel counterpart to the 🎤 dictation button.
  const ocrOn = !!(state.status && state.status.screenshot_ocr);
  els.terminalScreenshot.hidden = !ocrOn;

  // The 🔊 read-aloud button (issue #190) reveal/probe lives in
  // terminal-readaloud.js — it needs both the cheap status flag and a live
  // hub health probe (see revealReadAloudButton for why).
  revealReadAloudButton();

  // Mirror window uses a uniquely identifiable OS title so the launcher
  // can find this Edge --app window via EnumWindows and dismiss it
  // with WM_CLOSE on Stop & Close (issue #20).
  if (isMirror) announceMirrorWindow(sid, sessionTitle(session));

  // Source the terminal colours from the design tokens so they can't fork
  // from the stylesheet (issue #314). --term-bg/--term-fg are the
  // theme-invariant terminal-screen tokens (issue #355): the xterm surface
  // stays dark in both themes. Fallbacks equal the token values.
  const rootStyle = getComputedStyle(document.documentElement);
  const term = new window.Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 13,
    scrollback: 10000,
    theme: {
      background: rootStyle.getPropertyValue('--term-bg').trim() || '#0a0a0a',
      foreground: rootStyle.getPropertyValue('--term-fg').trim() || '#f3f3f3',
    },
  });
  let fit = null;
  if (!isMirror) {
    fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
  }
  try {
    term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
  } catch (_) { /* optional */ }
  term.open(els.terminalHost);

  // GPU-accelerated renderer. Falls back to the default DOM renderer
  // on any failure (no WebGL2, driver bug, OS reclaiming the context
  // under memory pressure). Without the fallback the terminal would
  // freeze; with it, worst case is same perf as before.
  let webgl = null;
  try {
    if (window.WebglAddon && window.WebglAddon.WebglAddon) {
      webgl = new window.WebglAddon.WebglAddon();
      webgl.onContextLoss(function () {
        try { webgl.dispose(); } catch (_) {}
        webgl = null;
      });
      term.loadAddon(webgl);
    }
  } catch (exc) {
    try { if (webgl) webgl.dispose(); } catch (_) {}
    webgl = null;
  }

  // Full-screen differential agents (Codex/Antigravity/Copilot ratatui)
  // drive the pan-not-reflow keyboard path (issue #264): on these the phone
  // pans the fixed canvas above the keyboard rather than resizing the PTY.
  // Resolved off the live /api/agents flag; an unknown/missing agent (or a
  // degraded fallback agents list) reads as non-fullscreen — Claude's
  // inline reflow (#135), the safe default.
  const knownAgent = (state.agents || []).find(function (a) {
    return a.id === session.agent;
  });
  const t = {
    sid: sid, ws: null, tt: tt, term: term, fit: fit, webgl: webgl,
    mirror: isMirror, retryCount: 0, giveUpAt: 0,
    retryTimer: null, visibilityListener: null, tapHandler: null,
    disposeTouch: null, composeOpen: false,
    isFullscreen: !!(knownAgent && knownAgent.fullscreen),
  };
  state.terminal = t;

  // Live-refresh the title while the overlay is open (issue #266). The main
  // sessions poll is paused under the overlay (main.js), so without this an
  // open terminal / PC mirror window stays stuck on its first-paint title when
  // the agent renames the conversation (or a first-prompt title lands). Reuses
  // SESSIONS_POLL_MS so the session-fetch cadence is unchanged whether the
  // overlay is open or closed — just a direct fetch with no Running-sessions
  // list re-render, which is what the main poll's pause deliberately avoids.
  t.titleTimer = setInterval(function () {
    if (t !== state.terminal) return;
    jsonApi('/api/claude-code/sessions').then(function (body) {
      if (t !== state.terminal) return;
      const s = (body.sessions || []).find(function (x) {
        return x.session_id === sid;
      });
      if (s) refreshTerminalTitle(t, s);
    }).catch(function () { /* best-effort; title just won't refresh */ });
  }, SESSIONS_POLL_MS);

  // Native iOS momentum (fling) scrolling on the phone (issue #23).
  // Skipped for the PC mirror window — it scrolls with a wheel and
  // should keep mouse text-selection.
  if (!isMirror) t.disposeTouch = enableNativeTouchScroll(term);

  function applySize() {
    if (isMirror) {
      // Match the phone's PTY dimensions; never touch the PTY itself.
      const s = (state.sessions || []).find(function (x) {
        return x.session_id === sid;
      });
      const cols = (s && s.cols) || session.cols || 120;
      const rows = (s && s.rows) || session.rows || 40;
      try { term.resize(cols, rows); } catch (_) {}
      return;
    }
    // Pin the overlay to the visual viewport when the keyboard is up so
    // its bottom edge lands at the top of the keyboard and the prompt
    // stays visible — then fit() reflows xterm to the smaller box
    // (issue #135). iOS doesn't just shrink the visual viewport for the
    // keyboard, it also shifts it *down* (visualViewport.offsetTop > 0)
    // to sweep the focused line into view; a position:fixed; inset:0
    // overlay is anchored to the layout-viewport top, so unless we match
    // that offset it slides up off-screen — clipping the top rows and
    // exposing a band of the page behind it just above the keyboard.
    // Track both the height and the offset. Released (back to CSS 100dvh)
    // when the keyboard hides. Must run *before* fit() so it measures the
    // new host size.
    const vp = window.visualViewport;
    const kbH = (vp && els.terminalOverlay)
      ? keyboardOverlayHeight(window.innerHeight, vp.height) : null;
    if (vp && els.terminalOverlay) {
      if (kbH != null) {
        els.terminalOverlay.style.height = kbH + 'px';
        els.terminalOverlay.style.bottom = 'auto';
        els.terminalOverlay.style.top = Math.round(vp.offsetTop || 0) + 'px';
      } else {
        els.terminalOverlay.style.height = '';
        els.terminalOverlay.style.bottom = '';
        els.terminalOverlay.style.top = '';
      }
    }
    // Full-screen differential agent + keyboard up: PAN, don't reflow
    // (issue #264). Keep the PTY at its stable size and translate the fixed
    // canvas up so the bottom row (the agent's prompt) sits above the
    // keyboard — no fit(), no resize frame, so ratatui is never SIGWINCHed
    // into repainting its whole frame on a keyboard open/close. The host is
    // overflow:hidden, so the panned-off top rows clip cleanly. Measuring
    // .xterm-screen (the rendered grid) is translate-invariant, so the pan
    // is stable across the repeated scroll/resize events iOS fires while the
    // keyboard animates. Claude (inline) and the keyboard-down/rotation case
    // fall through to the reflow path below, which also clears the pan.
    if (t.isFullscreen && kbH != null) {
      const screen = term.element &&
        term.element.querySelector('.xterm-screen');
      const contentH = screen ? screen.getBoundingClientRect().height : 0;
      const panY = terminalPanY(contentH, kbH);
      if (term.element) {
        term.element.style.transform = 'translateY(-' + panY + 'px)';
      }
      return;
    }
    if (term.element) term.element.style.transform = '';
    try { if (fit) fit.fit(); } catch (_) {}
    // Keep the prompt (bottom row) in view after a keyboard-driven
    // reflow, but only if the user hadn't scrolled up to read history.
    try {
      const b = term.buffer.active;
      if (b.viewportY >= b.baseY - 1) term.scrollToBottom();
    } catch (_) {}
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({
        type: 'resize', rows: term.rows, cols: term.cols,
      }));
    }
  }
  t.applySize = applySize;

  if (isMirror) {
    // The phone may rotate or resize — re-sync to its size periodically.
    t.sizeTimer = setInterval(function () {
      fetchSessions().then(applySize).catch(function () {});
    }, 2500);
  } else {
    setTimeout(applySize, 0);
    t.onWindowResize = applySize;
    window.addEventListener('resize', applySize);
    // iOS doesn't fire 'resize' when its chrome (URL bar / home
    // indicator) shows or hides — those changes ride on the
    // visualViewport API instead. Without re-fitting, xterm keeps
    // its old row count and the freed pixels show as a dead black
    // band at the bottom of the overlay.
    if (window.visualViewport) {
      t.onVisualViewport = applySize;
      window.visualViewport.addEventListener('resize', applySize);
      // Keyboard-driven shifts of visualViewport.offsetTop (iOS sweeping
      // the focused line into view) ride on 'scroll', not 'resize' — wire
      // it too so the overlay re-tracks the offset mid-sweep instead of
      // leaving a band of the page behind it above the keyboard (#135).
      window.visualViewport.addEventListener('scroll', applySize);
    }
  }

  term.onData(function (d) {
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: d }));
    }
  });

  connectWs(t);
}

export function closeTerminal() {
  const t = state.terminal;
  state.terminal = null;
  if (!t) return;
  // Drop compose state so a re-open never shows a stale bar/draft.
  resetComposeBar();
  clearReconnect(t);
  if (t.sizeTimer) clearInterval(t.sizeTimer);
  if (t.titleTimer) clearInterval(t.titleTimer);
  if (t.disposeTouch) { try { t.disposeTouch(); } catch (_) {} }
  if (t.onWindowResize) window.removeEventListener('resize', t.onWindowResize);
  if (t.onVisualViewport && window.visualViewport) {
    window.visualViewport.removeEventListener('resize', t.onVisualViewport);
    window.visualViewport.removeEventListener('scroll', t.onVisualViewport);
  }
  // Release any keyboard-driven override (issue #135) so the next open
  // starts from the CSS-driven full height and inset:0 origin.
  if (els.terminalOverlay) {
    els.terminalOverlay.style.height = '';
    els.terminalOverlay.style.bottom = '';
    els.terminalOverlay.style.top = '';
  }
  try { if (t.ws) { t.ws.onclose = null; t.ws.close(); } } catch (_) {}
  try { if (t.webgl) t.webgl.dispose(); } catch (_) {}
  try { if (t.term) t.term.dispose(); } catch (_) {}
}

// iOS PWA rubber-band lets the user drag the whole body while the
// terminal overlay is open, tucking the terminal header under the
// status bar. Pin the body with position:fixed and stash the scroll
// position so we can restore it on close. Idempotent — re-opens from
// the sessions list re-enter through openTerminal but the body must
// stay pinned with the original scrollY.
let _savedScrollY = 0;

function lockBodyScroll() {
  if (document.body.style.position === 'fixed') return;
  _savedScrollY = window.scrollY || window.pageYOffset || 0;
  const s = document.body.style;
  s.position = 'fixed';
  s.top = '-' + _savedScrollY + 'px';
  s.left = '0';
  s.right = '0';
  s.width = '100%';
}

function unlockBodyScroll() {
  if (document.body.style.position !== 'fixed') return;
  const s = document.body.style;
  s.position = '';
  s.top = '';
  s.left = '';
  s.right = '';
  s.width = '';
  window.scrollTo(0, _savedScrollY);
}

export function hideTerminal() {
  // Leaving the Coding tab silences any in-flight read-aloud (#190) — the
  // speech queue / hub audio is global and would otherwise keep talking
  // off-screen.
  stopReading();
  closeTerminal();
  closeKeysPopover();
  closeSpeakPopover();
  els.terminalOverlay.hidden = true;
  document.body.classList.remove('terminal-open');
  unlockBodyScroll();
  els.terminalHost.innerHTML = '';
  setTerminalStatus(null);
  fetchSessions().catch(function () {});
}

async function sendImage(file) {
  const t = state.terminal;
  if (!t || !file) return;
  // Compose bar open: ask the session-host to skip the paste-into-PTY
  // step (inline=1) and just return the stored path, so we can drop it
  // into the textarea for review-before-send — mirroring 📋 (issue #41).
  const inline = !!t.composeOpen;
  const fd = new FormData();
  fd.append('file', file, file.name || 'image.png');
  try {
    const tt = readTerminalToken();
    const res = await apiRaw(
      '/api/claude-code/sessions/' + encodeURIComponent(t.sid) + '/image' +
        (inline ? '?inline=1' : ''),
      { method: 'POST', terminalToken: tt, body: fd }
    );
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    if (inline) {
      const body = await res.json().catch(function () { return null; });
      const path = body && body.path;
      if (path) {
        const ta = els.terminalComposeInput;
        ta.setRangeText(path, ta.selectionStart, ta.selectionEnd, 'end');
        growComposeInput();
        ta.focus();
      }
      toast('🖼️ Image uploaded — path added to the compose bar.', 'good');
    } else {
      toast('🖼️ Image sent — its path was pasted into the prompt.', 'good');
      if (t.term) t.term.focus();
    }
  } catch (exc) {
    apiFailToast('Image failed', exc);
  }
}

// On-screen keys popover (issue #36): a D-pad of arrow/Esc/Tab/Enter
// keys for iPhone keyboards (SwiftKey etc.) that lack them, so Claude's
// TUI prompts are navigable from the phone. Each key sends the matching
// VT/xterm escape sequence over the same WS `input` channel as paste.
const KEY_BYTES = {
  up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  enter: '\r', esc: '\x1b', tab: '\t',
};

// Shift-modified variants (issue #137). The ⇧ key is a sticky toggle that
// simulates holding Shift, so the next key sent uses these sequences. Tab
// becomes back-tab (`\x1b[Z`) — that's Shift+Tab, the way Claude Code cycles
// permission modes — and the arrows get their xterm Shift CSI form (modifier
// 2). Esc/Enter have no standard Shift sequence, so they fall back to the
// plain KEY_BYTES entry below.
const SHIFT_KEY_BYTES = {
  tab: '\x1b[Z',
  up: '\x1b[1;2A', down: '\x1b[1;2B', right: '\x1b[1;2C', left: '\x1b[1;2D',
};

let _disposeKeysOutsideClick = null;
// Sticky-Shift state: stays engaged across taps (so ⇧ then Tab Tab Tab cycles
// modes) until ⇧ is tapped again or the popover closes.
let _shiftHeld = false;

function setShiftHeld(held) {
  _shiftHeld = held;
  if (!els.terminalKeysPopover) return;
  const btn = els.terminalKeysPopover.querySelector('.key-shift');
  if (btn) {
    btn.classList.toggle('active', held);
    btn.setAttribute('aria-pressed', held ? 'true' : 'false');
  }
}

function closeKeysPopover() {
  if (!els.terminalKeysPopover) return;
  els.terminalKeysPopover.hidden = true;
  setShiftHeld(false);
  if (_disposeKeysOutsideClick) {
    _disposeKeysOutsideClick();
    _disposeKeysOutsideClick = null;
  }
}

function openKeysPopover() {
  if (!els.terminalKeysPopover) return;
  els.terminalKeysPopover.hidden = false;
  if (!_disposeKeysOutsideClick) {
    _disposeKeysOutsideClick = bindOutsideClickToClose(
      els.terminalKeysPopover, els.terminalKeys, closeKeysPopover
    );
  }
}

function wireKeysPopover() {
  els.terminalKeys.addEventListener('click', function () {
    if (els.terminalKeysPopover.hidden) {
      openKeysPopover();
      // Opening the popover means the user is about to drive a prompt,
      // which lives at the tail — snap to the bottom like the ↓ button.
      const t = state.terminal;
      if (t && t.term) { try { t.term.scrollToBottom(); } catch (_) {} }
    } else {
      closeKeysPopover();
    }
  });
  // Delegated: the popover stays open across arrow/Tab taps so the user
  // can chain `↓ ↓ ↵`; Enter/Esc usually end a prompt, so they close it.
  els.terminalKeysPopover.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.key-btn');
    if (!btn) return;
    const key = btn.getAttribute('data-key');
    // ⇧ toggles the sticky-Shift state and sends nothing on its own; the
    // modifier applies to the next key tap (and stays held for chaining).
    if (key === 'shift') {
      setShiftHeld(!_shiftHeld);
      const t = state.terminal;
      if (t && t.term) t.term.focus();
      return;
    }
    const bytes = (_shiftHeld && SHIFT_KEY_BYTES[key]) || KEY_BYTES[key];
    if (!bytes) return;
    const t = state.terminal;
    if (t && t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: bytes }));
    }
    if (t && t.term) t.term.focus();
    if (bytes === '\r' || bytes === '\x1b') closeKeysPopover();
  });
}

export function wireTerminal() {
  els.terminalBack.addEventListener('click', hideTerminal);
  // 🛑 Stop-and-kill the session straight from the terminal view (issue
  // #253) — no need to go back to the list first. Resolve the open
  // session from state.sessions by sid; stopSession() confirms, then
  // hides the overlay when it stops the session we're viewing.
  els.terminalKill.addEventListener('click', function () {
    const t = state.terminal;
    if (!t) return;
    const s = (state.sessions || []).find(function (x) {
      return x.session_id === t.sid;
    });
    if (s) stopSession(s);
  });
  wireKeysPopover();
  wireCompose();
  els.terminalImage.addEventListener('click', function () {
    els.terminalImageInput.click();
  });
  els.terminalJumpEnd.addEventListener('click', function () {
    const t = state.terminal;
    if (!t || !t.term) return;
    try { t.term.scrollToBottom(); } catch (_) {}
    t.term.focus();
  });
  // 🔊 read-aloud control (issues #190, #210) — wiring lives in
  // terminal-readaloud.js, alongside the button's action menu and the
  // summary modal it opens.
  wireReadAloud();
  els.terminalPaste.addEventListener('click', async function () {
    const t = state.terminal;
    if (!t) return;
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return;
      // Compose bar open: drop the clipboard at the textarea caret so
      // the user can review/edit before Send — don't WS-send.
      if (t.composeOpen) {
        const ta = els.terminalComposeInput;
        ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
        growComposeInput();
        ta.focus();
        return;
      }
      if (!t.ws || t.ws.readyState !== WebSocket.OPEN) return;
      t.ws.send(JSON.stringify({ type: 'input', data: framePaste(t, text) }));
      if (t.term) t.term.focus();
    } catch (exc) {
      toast('Clipboard unavailable — paste manually', 'error');
    }
  });
  els.terminalImageInput.addEventListener('change', function () {
    const file = els.terminalImageInput.files && els.terminalImageInput.files[0];
    els.terminalImageInput.value = '';
    if (file) sendImage(file);
  });
  els.terminalHost.addEventListener('paste', function (ev) {
    const items = (ev.clipboardData && ev.clipboardData.items) || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf('image') === 0) {
        const file = items[i].getAsFile();
        if (file) { ev.preventDefault(); sendImage(file); return; }
      }
    }
  });
  els.terminalHost.addEventListener('dragover', function (ev) {
    ev.preventDefault();
  });
  els.terminalHost.addEventListener('drop', function (ev) {
    const file = ev.dataTransfer && ev.dataTransfer.files &&
      ev.dataTransfer.files[0];
    if (file && file.type && file.type.indexOf('image') === 0) {
      ev.preventDefault();
      sendImage(file);
    }
  });
}
