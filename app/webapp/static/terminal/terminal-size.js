/* Terminal sizing for terminal.js: fit the screen to its box, pin the overlay
 * above the phone keyboard, pan a canvas the box cannot show, and tell the PTY.
 *
 * Only the phone sizes the PTY; the PC mirror window follows the phone's size.
 */

import { els, state } from '../state.js';
import { fetchSessions } from '../sessions.js';
import { startPoll } from '../shared/poll.js';
import { beginRepaintBatch } from './terminal-connection.js';
// Circular with this module, like terminal.js: nothing here runs at import time.
import { inChatMode } from '../session-overlay.js';

// Given the layout-viewport height and the current visual-viewport
// height, return the pixel height to pin the terminal overlay to so its
// bottom edge sits at the top of the on-screen keyboard — or null to
// release the override and let the overlay fill the screen via the CSS
// (100dvh). iOS shrinks `visualViewport.height` when the software
// keyboard slides up but does NOT shrink the layout viewport, so a
// `position:fixed; inset:0` overlay keeps covering the whole screen
// *behind* the keyboard and the active prompt row renders hidden under
// it. Only a substantial shrink counts as the keyboard;
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
// (the visible "refreshment"). Instead we keep the PTY at its
// stable size and pan the fixed canvas: translate it up by the overflow so
// the bottom stays visible while the top scrolls off behind the chrome.
// Clamped at 0 so a canvas already shorter than the box never shifts down.
export function terminalPanY(contentHeight, visibleHeight) {
  if (!(contentHeight > 0) || !(visibleHeight > 0)) return 0;
  return Math.max(0, Math.round(contentHeight - visibleHeight));
}

// The PTY size the phone is willing to send. Every PTY resize
// SIGWINCHes an inline agent (Claude) into a full-viewport repaint, and the
// copy already in xterm's scrollback survives it — so each resize frame can
// land a duplicate of the visible conversation. The iOS keyboard sweep fires
// visualViewport events mid-animation, sampling 1- and 6-row sizes that are
// never a real viewport; floor them so no such frame reaches the PTY. The
// session-host clamps to the same floor (src/session_host.py PTY_MIN_*) for
// its non-WS callers.
export const PTY_MIN_ROWS = 8;
export const PTY_MIN_COLS = 20;

export function ptySendSize(rows, cols) {
  return {
    rows: Math.max(PTY_MIN_ROWS, Math.round(rows) || 0),
    cols: Math.max(PTY_MIN_COLS, Math.round(cols) || 0),
  };
}

// How long xterm's size must hold still before the resize frame goes out
// Only the frame is settled — fit()/pan/scrollToBottom still
// track the keyboard immediately — so a keyboard open/close cycle costs the
// agent two repaints instead of one per intermediate sample. Same order as
// the 350ms orientation settle below.
const RESIZE_SETTLE_MS = 300;

// Hand the overlay back to its CSS-driven full height and inset:0 origin.
export function releaseOverlayPin() {
  if (!els.terminalOverlay) return;
  els.terminalOverlay.style.height = '';
  els.terminalOverlay.style.bottom = '';
  els.terminalOverlay.style.top = '';
  delete els.terminalOverlay.dataset.keyboard;
}

// Fit the terminal to its box and tell the PTY. A stashed warm terminal
// is inert: its resize listeners stay bound while hidden, but only
// the ACTIVE terminal may touch the shared overlay chrome or its own layout.
// Resume re-runs applySize. Same while the Chat pane is showing: the
// host is display:none, so fit() would measure 0 rows/cols, floor to the PTY
// minimum and SIGWINCH the agent into a repaint (a known class of bug) — the
// switch back to Terminal re-runs applySize once against the real box.
export function applyTerminalSize(t, session) {
  if (t !== state.terminal || inChatMode()) return;
  if (t.mirror) {
    resizeMirrorTerm(t, session);
    panCanvas(t.term);
    return;
  }
  pinOverlayToKeyboard();
  if (t.isFullscreen && t.fsSized && t.lastSentSize) {
    growPinnedRows(t);
    panCanvas(t.term);
    return;
  }
  reflowTerminal(t);
}

// Match the phone's PTY dimensions; never touch the PTY itself.
function resizeMirrorTerm(t, session) {
  const s = (state.sessions || []).find(function (x) {
    return x.session_id === t.sid;
  });
  const cols = (s && s.cols) || session.cols || 120;
  const rows = (s && s.rows) || session.rows || 40;
  try { t.term.resize(cols, rows); } catch (_) {}
}

// Pin the overlay to the visual viewport when the keyboard is up so
// its bottom edge lands at the top of the keyboard and the prompt
// stays visible — then fit() reflows xterm to the smaller box
// iOS doesn't just shrink the visual viewport for the
// keyboard, it also shifts it *down* (visualViewport.offsetTop > 0)
// to sweep the focused line into view; a position:fixed; inset:0
// overlay is anchored to the layout-viewport top, so unless we match
// that offset it slides up off-screen — clipping the top rows and
// exposing a band of the page behind it just above the keyboard.
// Track both the height and the offset. Released (back to CSS 100dvh)
// when the keyboard hides. Must run *before* fit() so it measures the
// new host size.
function pinOverlayToKeyboard() {
  const vp = window.visualViewport;
  const kbH = (vp && els.terminalOverlay)
    ? keyboardOverlayHeight(window.innerHeight, vp.height) : null;
  if (!vp || !els.terminalOverlay) return;
  if (kbH != null) {
    els.terminalOverlay.style.height = kbH + 'px';
    els.terminalOverlay.style.bottom = 'auto';
    els.terminalOverlay.style.top = Math.round(vp.offsetTop || 0) + 'px';
    // Keyboard-up marker for CSS: the keyboard covers the home
    // indicator, so the bottom status strip drops its safe-area inset.
    // Set and cleared at exactly the sites that apply/undo this pin.
    els.terminalOverlay.dataset.keyboard = 'up';
  } else {
    releaseOverlayPin();
  }
}

// Full-screen differential agent: once sized, the PTY's columns are PINNED
// for the rest of the terminal's lifetime and its rows never shrink. A box
// that gets smaller (keyboard up, taller composer, browser chrome, rotation)
// PANS the canvas instead. Empirical (probe): Codex/ratatui re-emits its
// ENTIRE transcript on any winsize change (~65 KB on a long conversation), so
// a stray SIGWINCH replays the whole conversation through the phone terminal.
// Rows do grow when the box outgrows them: a terminal first sized while the
// keyboard was up would otherwise keep that short canvas after it closed.
// Growing only, a keyboard open/close cycle still resizes nothing. Pinned
// cols also make rotation harmless (iOS cannot lock orientation): landscape
// shows the portrait-width canvas letterboxed.
// (This path also requires a size to have been SENT — the pre-WS first fit
// sets fsSized, but the phone's authoritative size must still go out on the
// first open.)
function growPinnedRows(t) {
  let rows = 0;
  try { rows = t.fit.proposeDimensions().rows; } catch (_) { /* unmeasured */ }
  if (rows > t.term.rows) {
    const follow = followsTail(t);
    try { t.term.resize(t.term.cols, rows); } catch (_) { /* best effort */ }
    if (follow) { try { t.term.scrollToBottom(); } catch (_) {} }
  }
  // Also with nothing grown: a size grown while the socket was down goes out
  // on reconnect, and an unchanged one is deduped.
  queuePtySize(t);
}

// Shift the canvas up by whatever spills below the host's box, so the last
// row (the prompt) ends inside it; 0 clears the pan. The host is
// overflow:hidden, so panned-off top rows clip cleanly.
function panCanvas(term) {
  const screen = term.element &&
    term.element.querySelector('.xterm-screen');
  const contentH = screen ? screen.getBoundingClientRect().height : 0;
  // Pan against the HOST's real box, not the visual-viewport height:
  // the overlay also holds the header bar and the
  // compose bar, so the viewport height over-states the terminal's
  // visible box by their combined height — under-panning by exactly
  // that much and hiding the bottom rows (the prompt echo) behind
  // the compose bar whenever the native keyboard is up. The overlay
  // pinning above already resized the flex layout, so the host rect
  // is the box the canvas actually shows through.
  // The canvas sits inside .xterm's vertical padding, so that comes off too.
  const pad = term.element ? getComputedStyle(term.element) : null;
  const boxH = (pad && els.terminalHost) ?
    els.terminalHost.getBoundingClientRect().height -
      (parseFloat(pad.paddingTop) || 0) - (parseFloat(pad.paddingBottom) || 0) : 0;
  const panY = terminalPanY(contentH, boxH);
  if (term.element) {
    term.element.style.transform =
      panY ? ('translateY(-' + panY + 'px)') : '';
  }
}

// Whether the view was at the tail, asked before the fit. A box that just grew
// has already clamped the scroll position upward by up to the rows it gained,
// so those count as still at the tail.
function followsTail(t) {
  try {
    const next = t.fit && t.fit.proposeDimensions();
    const gained = next ? Math.max(0, next.rows - t.term.rows) : 0;
    const b = t.term.buffer.active;
    return b.viewportY >= b.baseY - 1 - gained;
  } catch (_) { return false; }
}

function reflowTerminal(t) {
  const term = t.term;
  if (term.element) term.element.style.transform = '';
  const follow = followsTail(t);
  try { if (t.fit) t.fit.fit(); } catch (_) {}
  // Keep the prompt (bottom row) in view after a keyboard- or composer-driven
  // reflow, but only if the user hadn't scrolled up to read history.
  if (follow) { try { term.scrollToBottom(); } catch (_) {} }
  queuePtySize(t);
  t.fsSized = true;
}

function sizeKey(size) {
  return size.rows + 'x' + size.cols;
}

// The first size on a terminal is the phone's authoritative one and goes out
// at once; every later change is settled so a keyboard sweep's intermediate
// samples never reach the PTY. Only a changed size restarts the settle: the
// host observer and the viewport events re-run the sizing without one, and one
// delivered late would otherwise keep postponing the frame.
function queuePtySize(t) {
  if (!t.ws || t.ws.readyState !== WebSocket.OPEN) return;
  const key = sizeKey(ptySendSize(t.term.rows, t.term.cols));
  if (t.resizeSettleTimer && key === t.queuedSize) return;
  t.queuedSize = key;
  if (t.resizeSettleTimer) clearTimeout(t.resizeSettleTimer);
  t.resizeSettleTimer = null;
  if (t.lastSentSize) {
    t.resizeSettleTimer = setTimeout(function () { sendPtySize(t); },
      RESIZE_SETTLE_MS);
  } else {
    sendPtySize(t);
  }
}

function sendPtySize(t) {
  t.resizeSettleTimer = null;
  if (!t.ws || t.ws.readyState !== WebSocket.OPEN) return;
  // Same-size dedupe: a resize frame that changes nothing still
  // costs a setwinsize round-trip; skip it. Compared on the floored size,
  // so a settled sweep that lands back where it started sends nothing. A
  // real change on a fullscreen agent triggers the transcript
  // re-emission — batch it into a single paint (terminal-connection.js).
  const size = ptySendSize(t.term.rows, t.term.cols);
  const key = sizeKey(size);
  if (key === t.lastSentSize) return;
  const resized = !!t.lastSentSize;
  t.lastSentSize = key;
  if (t.isFullscreen) beginRepaintBatch(t, resized);
  t.ws.send(JSON.stringify({
    type: 'resize', rows: size.rows, cols: size.cols,
  }));
}

// Keep the terminal sized to its box for as long as it lives: the mirror
// polls the phone's size, the phone listens to every viewport change.
export function wireSizeTracking(t) {
  const applySize = t.applySize;
  if (t.mirror) {
    // The phone may rotate or resize — re-sync to its size periodically.
    t.stopSizePoll = startPoll(function () {
      return fetchSessions().then(applySize);
    }, 2500);
    return;
  }
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
    // leaving a band of the page behind it above the keyboard.
    window.visualViewport.addEventListener('scroll', applySize);
  }
  // A portrait/landscape rotation cycle can leave
  // window.innerHeight and visualViewport.height transiently mismatched
  // while iOS's chrome bars settle, so a 'resize'/'visualViewport resize'
  // event mid-rotation can sample keyboardOverlayHeight() at a bad
  // moment and pin the overlay to a stale shrunk height — with no
  // guaranteed later event to release it, leaving the session list/
  // Projects grid bleeding through underneath. 'orientationchange' fires
  // once per rotation regardless of that race: release any pin
  // immediately so the overlay is never stuck small, then re-run
  // applySize() once the viewport has settled so a keyboard that's
  // genuinely still open gets correctly re-pinned.
  t.onOrientationChange = function () {
    // Stashed (warm-cached) terminals keep this listener bound
    // while hidden — same as applySize()'s own guard, only the ACTIVE
    // terminal may touch the shared overlay chrome, and not while the
    // Chat pane owns it.
    if (t !== state.terminal || inChatMode()) return;
    releaseOverlayPin();
    if (t.orientationSettleTimer) clearTimeout(t.orientationSettleTimer);
    t.orientationSettleTimer = setTimeout(applySize, 350);
  };
  window.addEventListener('orientationchange', t.onOrientationChange);
}

// The host's box also changes with no viewport event: the composer under it
// grows with its draft and shrinks on send. Re-run the active terminal's
// sizing on every such change; applyTerminalSize settles the PTY frame and
// ignores the Chat pane. A hidden host measures 0 and is left alone.
export function watchHostSize() {
  if (!window.ResizeObserver) return;
  new ResizeObserver(function () {
    const t = state.terminal;
    if (t && t.applySize && els.terminalHost.clientHeight) t.applySize();
  }).observe(els.terminalHost);
}

// Drop every listener and timer wireSizeTracking and the resize settle armed.
export function unwireSizeTracking(t) {
  if (t.stopSizePoll) t.stopSizePoll();
  if (t.onWindowResize) window.removeEventListener('resize', t.onWindowResize);
  if (t.onVisualViewport && window.visualViewport) {
    window.visualViewport.removeEventListener('resize', t.onVisualViewport);
    window.visualViewport.removeEventListener('scroll', t.onVisualViewport);
  }
  if (t.onOrientationChange) {
    window.removeEventListener('orientationchange', t.onOrientationChange);
  }
  if (t.orientationSettleTimer) clearTimeout(t.orientationSettleTimer);
  if (t.resizeSettleTimer) clearTimeout(t.resizeSettleTimer);
}
