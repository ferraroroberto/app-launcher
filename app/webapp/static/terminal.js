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
 * This module coordinates the xterm instance, its cache, sizing and the
 * keyboard-pan. The WebSocket lifecycle lives in terminal-connection.js and
 * theme resolution in terminal-theme.js. Five sibling concerns are split
 * out: the compose bar + dictation + OCR (terminal-compose.js), read-aloud
 * UI (terminal-readaloud.js, atop the terminal-readback.js engine) and the
 * PC-mirror-window title/guard logic (terminal-mirror.js) by issue #315,
 * then the on-screen keys D-pad (terminal-keys.js) and image paste / drop /
 * attach (terminal-image.js) by issue #723.
 */

import { els, state, SESSIONS_POLL_MS } from './state.js';
import { apiFailToast, isDesktopClient, jsonApi, toast } from './api.js';
import { fetchSessions, sessionTitle, stopSession } from './sessions.js';
import { enableNativeTouchScroll } from './terminal-touch.js';
import {
  announceMirrorWindow,
  isMirrorWindowSession,
  mirrorDocTitle,
  refreshTerminalTitle,
  setTerminalTitleText,
} from './terminal-mirror.js';
import {
  framePaste,
  resetComposeBar,
  sendSubmit,
  wireCompose,
  growComposeInput,
} from './terminal-compose.js';
import { closeKeysPopover, wireKeysPopover } from './terminal-keys.js';
import { wireTerminalImage } from './terminal-image.js';
import { closeSpeakPopover, revealReadAloudButton, stopReading, wireReadAloud } from './terminal-readaloud.js';
import {
  beginRepaintBatch,
  clearTerminalReconnect,
  connectTerminalWs,
  flushRepaintBatch,
  routeFrame,
  setTerminalStatus,
} from './terminal-connection.js';
import {
  applyTermTheme,
  setUserTermThemes,
  termContrastRatio,
  termScreenTheme,
} from './terminal-theme.js';
import { voiceDictationAvailable } from './voice.js';
import { ensureTerminalToken } from './webauthn.js';

// Test seam (#20/#135/#166/#181/#264): several pure helpers below are
// imported directly by the e2e suite via `import('/static/terminal.js')`
// (terminalPanY, keyboardOverlayHeight, sendSubmit, framePaste, routeFrame,
// mirrorDocTitle). Re-export the ones that moved to a split module so those
// imports keep working unchanged.
export {
  framePaste,
  mirrorDocTitle,
  routeFrame,
  sendSubmit,
  setUserTermThemes,
  termContrastRatio,
  termScreenTheme,
};

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

// Desktop-vs-phone launch-size contract, shared by every PTY-launch call
// site (Coding tab, Board issue-start/dispatch, Life OS recap/skill launch,
// issue #374): a desktop browser gets the dedicated PC mirror window sized
// to its own default, a phone carries its real terminal size so the PTY's
// first frame is painted at the right width. Mutates `payload` in place —
// callers apply their own outer gate (e.g. "only for a streamed/pty launch")
// before calling this.
//
// `in_page: true` (issue #609) is the explicit "I will render this myself"
// signal the server's `should_mirror_to_pc` now requires from a genuine SPA
// loopback client, instead of inferring it from "loopback and no desktop
// flag" — an inference a non-browser loopback API caller (a script, the
// fleet chief) also matched, silently leaving it with no window at all. A
// phone sets it too, but it's moot there: the server already mirrors any
// non-loopback caller before this flag is ever consulted.
export function applyLaunchSizePayload(payload) {
  if (isDesktopClient()) {
    payload.desktop = true;
  } else {
    payload.in_page = true;
    const sz = estimateTermSize();
    payload.rows = sz.rows;
    payload.cols = sz.cols;
  }
}

// The post-launch tail shared by every PTY-launch response handler (Coding
// tab's launchApp, Life OS's launchRecap/launchSkill): refresh the sessions
// list, then drop straight into the in-page terminal for a full-control
// (non-'remote') session on a phone — a desktop browser already got its
// dedicated PC Edge window instead (issue #241), so it stays on the SPA.
// No-op when the launch produced no session (a non-claude-code app launch).
export function handleLaunchResponse(session) {
  if (!session) return;
  fetchSessions().catch(function () {});
  if (session.kind !== 'remote' && !isDesktopClient()) {
    openTerminal(session);
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

// Warm-terminal cache (#430 round 3): sid → the live terminal object.
// Closing the overlay does NOT dispose the xterm or the WebSocket any
// more — the painted frame and the stream stay warm so re-opening a
// session shows its tail instantly, with no re-subscribe and therefore
// no server-side repaint nudge (the nudge makes ratatui agents re-emit
// their whole transcript, which also flashed the always-on PC mirror on
// every phone re-open). Insertion order doubles as LRU; the cap bounds
// WebGL contexts + scrollback memory on the phone.
const _termCache = new Map();
const _TERM_CACHE_MAX = 3;

// Hide the active terminal without tearing it down: its WS, timers and
// listeners keep running (guards on `state.terminal` make them inert),
// its canvas is just display:none'd until the next openTerminal(sid).
function stashActiveTerminal() {
  const t = state.terminal;
  state.terminal = null;
  if (!t) return;
  // Drop compose state so a re-open never shows a stale bar/draft.
  resetComposeBar();
  if (t.term && t.term.element) t.term.element.style.display = 'none';
  // Release any keyboard-driven override (issue #135) so the next open
  // starts from the CSS-driven full height and inset:0 origin.
  if (els.terminalOverlay) {
    els.terminalOverlay.style.height = '';
    els.terminalOverlay.style.bottom = '';
    els.terminalOverlay.style.top = '';
  }
}

// Full teardown of one terminal (cache eviction, session gone, mirror
// shutdown). The reconnect/batch cleanup runs BEFORE term.dispose() so a
// pending repaint-batch timer can never write into a disposed xterm.
function disposeTerminal(t) {
  if (!t) return;
  if (state.terminal === t) {
    state.terminal = null;
    resetComposeBar();
  }
  _termCache.delete(t.sid);
  clearTerminalReconnect(t);
  if (t.sizeTimer) clearInterval(t.sizeTimer);
  if (t.titleTimer) clearInterval(t.titleTimer);
  if (t.disposeTouch) { try { t.disposeTouch(); } catch (_) {} }
  if (t.onWindowResize) window.removeEventListener('resize', t.onWindowResize);
  if (t.onVisualViewport && window.visualViewport) {
    window.visualViewport.removeEventListener('resize', t.onVisualViewport);
    window.visualViewport.removeEventListener('scroll', t.onVisualViewport);
  }
  if (t.onOrientationChange) {
    window.removeEventListener('orientationchange', t.onOrientationChange);
  }
  if (t.orientationSettleTimer) clearTimeout(t.orientationSettleTimer);
  try { if (t.ws) { t.ws.onclose = null; t.ws.close(); } } catch (_) {}
  try { if (t.webgl) t.webgl.dispose(); } catch (_) {}
  const el = t.term ? t.term.element : null;
  try { if (t.term) t.term.dispose(); } catch (_) {}
  try { if (el && el.parentNode) el.parentNode.removeChild(el); } catch (_) {}
}

// Evict cached terminals whose session no longer exists (stopped from the
// list, agent exited). Best-effort against the last sessions poll.
function pruneTermCache() {
  const entries = Array.from(_termCache.values());
  for (const ct of entries) {
    const alive = (state.sessions || []).some(function (x) {
      return x.session_id === ct.sid;
    });
    if (!alive && ct !== state.terminal) disposeTerminal(ct);
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
    stashActiveTerminal();
    els.terminalOverlay.hidden = false;
    document.body.classList.add('terminal-open');
    lockBodyScroll();
    setTerminalTitleText(session);
    setTerminalStatus(
      state.status.terminal.reason ||
        'The live terminal is Tailscale-only.',
      { icon: 'lock' }
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
  stashActiveTerminal();
  pruneTermCache();
  els.terminalOverlay.hidden = false;
  document.body.classList.add('terminal-open');
  lockBodyScroll();
  // Use the same stripping sessionTitle() applies elsewhere so Claude's
  // leading ✻/☁️/emoji prefix doesn't show up on first paint — the
  // agent icon next to the title is the redundancy. Also prepends the
  // chief's crown marker (#547) when this session is the chief.
  setTerminalTitleText(session);
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
  els.terminalRecord.hidden = !voiceDictationAvailable();

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

  // Warm re-open (#430): the session's terminal is still alive from a
  // previous open — show its already-painted frame and reuse its WS.
  // Nothing is re-subscribed, so the session-host never fires the repaint
  // nudge and ratatui never re-emits the transcript. Only if the WS died
  // while stashed (iOS killed it in the background) do we reconnect,
  // which falls back to the concealed clear+repaint path.
  const cached = _termCache.get(sid);
  if (cached && cached.term) {
    _termCache.delete(sid);
    _termCache.set(sid, cached);
    cached.tt = tt;
    state.terminal = cached;
    if (cached.term.element) cached.term.element.style.display = '';
    applyTermTheme();
    const wsAlive = cached.ws &&
      (cached.ws.readyState === WebSocket.CONNECTING ||
       cached.ws.readyState === WebSocket.OPEN);
    if (wsAlive) {
      if (cached.ws.readyState === WebSocket.OPEN) setTerminalStatus(null);
      if (cached.applySize) cached.applySize();
      try {
        cached.term.refresh(0, cached.term.rows - 1);
        cached.term.scrollToBottom();
      } catch (_) { /* best effort */ }
      cached.term.focus();
    } else {
      cached.retryCount = 0;
      cached.giveUpAt = 0;
      connectTerminalWs(cached);
    }
    return;
  }

  // Source the terminal colours from the design tokens so they can't fork
  // from the stylesheet (issue #314). --term-bg/--term-fg are the
  // terminal-screen tokens (issue #355), following the app theme
  // (issue #383) — resolved by termScreenTheme(), which also carries the
  // light ANSI palette.
  const term = new window.Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 13,
    scrollback: 10000,
    theme: termScreenTheme(),
    // Per-cell contrast floor for the light screen (issue #381); 1 (off)
    // on the default dark screen.
    minimumContrastRatio: termContrastRatio(),
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
  // inline reflow (#135), the safe default. Board drill-down and ?session=
  // deep links pass a synthetic {session_id, name} with no agent field
  // (#430) — resolve it from the sessions list so a Codex session opened
  // from the Board still gets the fullscreen client handling.
  const liveSession = (state.sessions || []).find(function (x) {
    return x.session_id === sid;
  });
  const agentId = session.agent || (liveSession && liveSession.agent) || '';
  const knownAgent = (state.agents || []).find(function (a) {
    return a.id === agentId;
  });
  const t = {
    sid: sid, ws: null, tt: tt, term: term, fit: fit, webgl: webgl,
    mirror: isMirror, retryCount: 0, giveUpAt: 0,
    retryTimer: null, visibilityListener: null, tapHandler: null,
    disposeTouch: null, composeOpen: false, composeHasImage: false,
    isFullscreen: !!(knownAgent && knownAgent.fullscreen),
    // Resize-dedupe + repaint-batch state (#430). lastSentSize suppresses
    // same-size resize frames; fsSized gates the fullscreen pan path until
    // the first real fit has run; batch* is owned by terminal-connection.js.
    lastSentSize: null, fsSized: false,
    batchBuf: null, batchTimer: null, batchQuietTimer: null, batchDeadline: 0,
    onShutdown: closeTerminal,
  };
  state.terminal = t;
  // Register in the warm cache (#430) and evict the least-recently-used
  // beyond the cap — a full teardown, so its WS/GL context are released.
  _termCache.set(sid, t);
  while (_termCache.size > _TERM_CACHE_MAX) {
    const oldest = _termCache.values().next().value;
    if (!oldest || oldest === t) break;
    disposeTerminal(oldest);
  }
  // Sync the overlay chrome with any user background override (#381) —
  // the constructor already resolved theme + contrast for xterm itself.
  applyTermTheme();

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
    // A stashed warm terminal (#430) is inert: its resize listeners stay
    // bound while hidden, but only the ACTIVE terminal may touch the
    // shared overlay chrome or its own layout. Resume re-runs applySize.
    if (t !== state.terminal) return;
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
    // Full-screen differential agent: once sized, the PTY is PINNED for the
    // rest of the session's lifetime — no further reflow, ever (#432b).
    // EVERY change after the first real size (keyboard up/down, compose
    // bar, browser chrome, a PWA layout-viewport shrink, OR a phone
    // rotation) PANS/letterboxes the fixed-size canvas instead (#264,
    // #430, #432). Empirical (#430 probe): Codex/ratatui re-emits its
    // ENTIRE transcript on any winsize change — rows or cols, either
    // direction (~65 KB on a long conversation) — so a single stray
    // SIGWINCH replays the whole conversation through the phone terminal.
    // iOS supports neither the manifest `orientation` member nor
    // `ScreenOrientation.lock()`, so rotation can't be blocked at the
    // platform level — pinning cols makes it harmless instead: landscape
    // just shows the same portrait-width canvas letterboxed (xterm sizes
    // its own canvas off `cols`, not the host box, so no extra CSS is
    // needed). The host is overflow:hidden, so panned-off top rows clip
    // cleanly; panning by the content-vs-box overflow also covers the
    // keyboard-down case (overflow 0 → transform cleared). Claude (inline)
    // and the fullscreen first-fit case fall through to the reflow path
    // below, which also clears the pan.
    // (The pan shortcut additionally requires a size to have been SENT —
    // the pre-WS first fit sets fsSized, but the phone's authoritative
    // size must still go out on the first open.)
    if (t.isFullscreen && t.fsSized && t.lastSentSize) {
      const screen = term.element &&
        term.element.querySelector('.xterm-screen');
      const contentH = screen ? screen.getBoundingClientRect().height : 0;
      // Pan against the HOST's real box, not the visual-viewport height
      // (#430 round 2): the overlay also holds the header bar and the
      // compose bar, so the viewport height over-states the terminal's
      // visible box by their combined height — under-panning by exactly
      // that much and hiding the bottom rows (the prompt echo) behind
      // the compose bar whenever the native keyboard is up. The overlay
      // pinning above already resized the flex layout, so the host rect
      // is the box the canvas actually shows through.
      const boxH = els.terminalHost ?
        els.terminalHost.getBoundingClientRect().height : 0;
      const panY = terminalPanY(contentH, boxH);
      if (term.element) {
        term.element.style.transform =
          panY ? ('translateY(-' + panY + 'px)') : '';
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
      // Same-size dedupe (#430): a resize frame that changes nothing
      // still costs a setwinsize round-trip; skip it. A real change on a
      // fullscreen agent triggers the transcript re-emission — batch it
      // into a single paint (terminal-connection.js).
      const size = term.rows + 'x' + term.cols;
      if (size !== t.lastSentSize) {
        t.lastSentSize = size;
        if (t.isFullscreen) beginRepaintBatch(t);
        t.ws.send(JSON.stringify({
          type: 'resize', rows: term.rows, cols: term.cols,
        }));
      }
    }
    t.fsSized = true;
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
    // A portrait/landscape rotation cycle (issue #446) can leave
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
      // Stashed (warm-cached, #430) terminals keep this listener bound
      // while hidden — same as applySize()'s own guard, only the ACTIVE
      // terminal may touch the shared overlay chrome.
      if (t !== state.terminal) return;
      if (els.terminalOverlay) {
        els.terminalOverlay.style.height = '';
        els.terminalOverlay.style.bottom = '';
        els.terminalOverlay.style.top = '';
      }
      if (t.orientationSettleTimer) clearTimeout(t.orientationSettleTimer);
      t.orientationSettleTimer = setTimeout(applySize, 350);
    };
    window.addEventListener('orientationchange', t.onOrientationChange);
  }

  term.onData(function (d) {
    // Typing during a repaint batch (#430): flush first so the echo isn't
    // held back behind the batch's quiet-gap/deadline window.
    flushRepaintBatch(t);
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: d }));
    }
  });

  connectTerminalWs(t);
}

// Full teardown of the ACTIVE terminal — kept for the paths where the
// session itself is over (mirror shutdown frame, stop-from-terminal): a
// warm cache entry would only be a zombie there.
export function closeTerminal() {
  const t = state.terminal;
  if (!t) return;
  disposeTerminal(t);
  // Release any keyboard-driven override (issue #135) so the next open
  // starts from the CSS-driven full height and inset:0 origin.
  if (els.terminalOverlay) {
    els.terminalOverlay.style.height = '';
    els.terminalOverlay.style.bottom = '';
    els.terminalOverlay.style.top = '';
  }
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
  // Stash, don't dispose (#430): the xterm keeps its painted frame and
  // the WS keeps streaming, so re-opening this session is instant and
  // never triggers the server's repaint nudge. The host's DOM is NOT
  // cleared for the same reason.
  stashActiveTerminal();
  closeKeysPopover();
  closeSpeakPopover();
  els.terminalOverlay.hidden = true;
  document.body.classList.remove('terminal-open');
  unlockBodyScroll();
  setTerminalStatus(null);
  fetchSessions().catch(function () {});
}

export function wireTerminal() {
  // The terminal screen follows the app theme (issue #383): live-restyle
  // the open terminal whenever the app theme flips — xterm colors live in
  // the renderer, so CSS alone can't restyle an already-open screen;
  // options.theme can.
  new MutationObserver(applyTermTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });
  // User theme file (issue #381) — best-effort; the built-ins are already
  // a complete theme, so a missing/failed fetch changes nothing.
  jsonApi('/api/terminal-themes').then(function (body) {
    setUserTermThemes(body && body.themes);
    applyTermTheme();
  }).catch(function () { /* built-ins stand */ });
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
  wireTerminalImage();
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
}
