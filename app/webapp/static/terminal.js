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
 * This module coordinates the xterm instance and its cache. Sizing and the
 * keyboard-pan live in terminal-size.js, the WebSocket lifecycle in
 * terminal-connection.js and theme resolution in terminal-theme.js. Five sibling concerns are split
 * out: the compose bar + dictation + OCR (terminal-compose.js), read-aloud
 * UI (terminal-readaloud.js, atop the terminal-readback.js engine) and the
 * PC-mirror-window title/guard logic (terminal-mirror.js),
 * then the on-screen keys D-pad (terminal-keys.js) and image paste / drop /
 * attach (terminal-image.js). The bar's ⋮ session menu and
 * the floating Latest pill live in terminal-bar.js. This
 * module is the *terminal pane* of the session overlay: session-overlay.js
 * decides which pane (terminal / chat) shows and calls attachTerminalPane()
 * when the terminal is wanted; the shell (overlay visible, body locked,
 * title set) is shared by both panes via showSessionShell(). openTerminal()
 * stays as the Terminal-mode entry every launch tail and deep link uses.
 */

import { els, state, SESSIONS_POLL_MS } from './state.js';
import {
  keyboardOverlayHeight,
  PTY_MIN_COLS,
  PTY_MIN_ROWS,
  ptySendSize,
  releaseOverlayPin,
  terminalPanY,
  unwireSizeTracking,
  watchHostSize,
  wireSizeTracking,
  applyTerminalSize,
} from './terminal/terminal-size.js';
import { apiFailToast, jsonApi, opensInPcWindow } from './api.js';
import { fetchSessions, sessionTitle } from './sessions.js';
import { enableNativeTouchScroll } from './terminal/terminal-touch.js';
import { loadXterm } from './terminal/xterm-loader.js';
import {
  announceMirrorWindow,
  dropEarlyMirrorMarker,
  isMirrorWindowSession,
  mirrorDocTitle,
  refreshTerminalTitle,
  setTerminalTitleText,
} from './terminal/terminal-mirror.js';
import {
  framePaste,
  mountTerminalComposer,
  resetComposeBar,
  sendSubmit,
  terminalComposer,
} from './terminal/terminal-compose.js';
import { wireTerminalImage } from './terminal/terminal-image.js';
import {
  closeTerminalMenu,
  updateLatestPill,
  watchLatestPill,
  wireTerminalMenu,
} from './terminal/terminal-bar.js';
import { refreshReadAloudAvailability, stopReading, wireReadAloud } from './terminal/terminal-readaloud.js';
import {
  clearTerminalReconnect,
  connectTerminalWs,
  flushRepaintBatch,
  markKeystroke,
  routeFrame,
  setTerminalStatus,
} from './terminal/terminal-connection.js';
import {
  applyTermTheme,
  setUserTermThemes,
  termContrastRatio,
  termScreenTheme,
} from './terminal/terminal-theme.js';
import { voiceDictationAvailable } from './voice.js';
import { isWideLayout } from './layout.js';
import { ensureTerminalToken } from './webauthn.js';
import { startPoll } from './_vendored/poll/poll.js';
// Mode routing. Circular with this module by design (same shape as
// sessions.js ↔ terminal.js): nothing here runs at import time.
import {
  closeSessionOverlay,
  openSessionOverlay,
  wireSessionModeToggle,
} from './session-overlay.js';

// Test seam: several pure helpers below are
// imported directly by the e2e suite via `import('/static/terminal.js')`
// (terminalPanY, keyboardOverlayHeight, sendSubmit, framePaste, routeFrame,
// mirrorDocTitle). Re-export the ones that moved to a split module so those
// imports keep working unchanged.
export {
  framePaste,
  keyboardOverlayHeight,
  mirrorDocTitle,
  PTY_MIN_COLS,
  PTY_MIN_ROWS,
  ptySendSize,
  routeFrame,
  sendSubmit,
  setUserTermThemes,
  termContrastRatio,
  terminalPanY,
  termScreenTheme,
};

// The post-launch tail shared by every PTY-launch response handler (Coding
// tab's launchApp, Life OS's launchRecap/launchSkill): refresh the sessions
// list, then drop straight into the in-page terminal for a full-control
// (non-'remote') session on a phone — a desktop browser already got its
// dedicated PC Edge window instead (the setting is on), so it stays on the SPA.
// No-op when the launch produced no session (a non-claude-code app launch).
export function handleLaunchResponse(session) {
  if (!session) return;
  fetchSessions().catch(function () {});
  if (session.kind !== 'remote' && !opensInPcWindow()) {
    openTerminal(session);
  }
}

// Warm-terminal cache: sid → the live terminal object.
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
// its canvas is just display:none'd until the next attachTerminalPane(sid).
// Exported for session-overlay.js: opening another session straight into
// Chat must stash the previous session's terminal too, or its title poll
// would keep writing the old title over the new session's bar.
export function stashActiveTerminal() {
  const t = state.terminal;
  state.terminal = null;
  if (!t) return;
  // Drop compose state so a re-open never shows a stale bar/draft.
  resetComposeBar();
  if (t.term && t.term.element) t.term.element.style.display = 'none';
  updateLatestPill();
  // Release any keyboard-driven override so the next open
  // starts from the CSS-driven full height and inset:0 origin.
  releaseOverlayPin();
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
  // Cancels a screen-first history pass still waiting to parse.
  t.historyGen = (t.historyGen || 0) + 1;
  unwireSizeTracking(t);
  if (t.stopTitlePoll) t.stopTitlePoll();
  if (t.disposeTouch) { try { t.disposeTouch(); } catch (_) {} }
  if (t.disposeLatest) { try { t.disposeLatest(); } catch (_) {} }
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

// The overlay shell both panes share: visible, body pinned against
// iOS rubber-band, title set. Idempotent — re-opens from the sessions list
// and mode switches re-enter through here.
export function showSessionShell(session) {
  els.terminalOverlay.hidden = false;
  document.body.classList.add('terminal-open');
  lockBodyScroll();
  // Use the same stripping sessionTitle() applies elsewhere so Claude's
  // leading ✻/☁️/emoji prefix doesn't show up on first paint — the
  // agent icon next to the title is the redundancy. Also prepends the
  // chief's crown marker when this session is the chief.
  setTerminalTitleText(session);
}

// Open the session overlay in Terminal mode — the entry every PTY-launch
// tail, ?terminal=/?session= deep link and Board drill-down uses.
// session-overlay.js owns the mode; it calls attachTerminalPane() below.
export function openTerminal(session) {
  openSessionOverlay(session, 'terminal');
}

// The terminal token, once the xterm stack is loaded too; null (after a
// toast naming which one failed) when either cannot be had. The status
// reads "Connecting…" while xterm downloads, so the open overlay is never
// a blank pane on a first open.
async function terminalPrereqs() {
  let tt = '';
  try {
    tt = await ensureTerminalToken();
  } catch (exc) {
    apiFailToast('Passkey unlock failed', exc);
    return null;
  }
  setTerminalStatus('Connecting…');
  try {
    await loadXterm();
  } catch (exc) {
    setTerminalStatus('Terminal failed to load');
    apiFailToast('Terminal failed to load', exc);
    return null;
  }
  return tt;
}

// Connect (or warm-resume) the terminal pane for `session` inside the
// already-shown overlay shell.
export async function attachTerminalPane(session) {
  const sid = session.session_id;
  if (!sid) return;

  // The live terminal is Tailscale-only. If this connection can't reach
  // it (public Cloudflare tunnel, off-tailnet Wi-Fi), explain that up
  // front instead of opening a terminal that only says "Disconnected".
  if (state.status && state.status.terminal &&
      state.status.terminal.reachable === false) {
    showUnreachableTerminal(session);
    return;
  }

  const tt = await terminalPrereqs();
  if (tt === null) return;
  stashActiveTerminal();
  pruneTermCache();
  showSessionShell(session);
  setTerminalStatus('Connecting…');

  // The PC mirror window is the launcher-spawned Edge --app window (issue
  // see terminal-mirror.js for why this can't just be the loopback
  // reason check). It renders whatever size the phone set and never resizes
  // the PTY — the phone is the single size authority, so the two clients
  // never fight (the server also ignores resize frames from role=pc).
  const isMirror = isMirrorWindowSession();
  syncPaneChrome(session, isMirror);

  // Warm re-open: the session's terminal is still alive from a
  // previous open — show its already-painted frame and reuse its WS.
  // Nothing is re-subscribed, so the session-host never fires the repaint
  // nudge and ratatui never re-emits the transcript. Only if the WS died
  // while stashed (iOS killed it in the background) do we reconnect,
  // which falls back to the concealed clear+repaint path.
  const cached = _termCache.get(sid);
  if (cached && cached.term) {
    resumeCachedTerminal(sid, cached, tt);
    return;
  }

  const xterm = openXterm(isMirror);
  const t = {
    sid: sid, ws: null, tt: tt, term: xterm.term, fit: xterm.fit,
    webgl: xterm.webgl,
    mirror: isMirror, retryCount: 0, giveUpAt: 0,
    retryTimer: null, visibilityListener: null, tapHandler: null,
    disposeTouch: null, disposeLatest: null,
    isFullscreen: isFullscreenAgent(session, sid),
    // Resize-dedupe + repaint-batch state. lastSentSize suppresses
    // same-size resize frames; fsSized gates the fullscreen pan path until
    // the first real fit has run; batch* is owned by terminal-connection.js.
    // resizeSettleTimer holds a pending settled resize frame, queuedSize the
    // size it waits on.
    lastSentSize: null, queuedSize: null, fsSized: false, resizeSettleTimer: null,
    batchBuf: null, batchTimer: null, batchQuietTimer: null, batchDeadline: 0,
    onShutdown: closeTerminal,
  };
  state.terminal = t;
  registerWarmTerminal(t);
  // Sync the overlay chrome with any user background override —
  // the constructor already resolved theme + contrast for xterm itself.
  applyTermTheme();

  startTitleRefresh(t);

  // Native iOS momentum (fling) scrolling on the phone.
  // Skipped for the PC mirror window — it scrolls with a wheel and
  // should keep mouse text-selection.
  if (!isMirror) t.disposeTouch = enableNativeTouchScroll(t.term);
  t.disposeLatest = watchLatestPill(t);

  t.applySize = function () { applyTerminalSize(t, session); };
  wireSizeTracking(t);

  t.term.onData(function (d) {
    // Typing during a repaint batch: flush first so the echo isn't
    // held back behind the batch's quiet-gap/deadline window.
    flushRepaintBatch(t);
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: d }));
      markKeystroke(t);
    }
  });

  connectTerminalWs(t);
}

// The unreachable-origin face of the pane: a lock icon and the reason,
// no terminal.
function showUnreachableTerminal(session) {
  stashActiveTerminal();
  showSessionShell(session);
  // An unreachable origin is never a mirror: shed a ?terminal= marker.
  dropEarlyMirrorMarker();
  setTerminalStatus(
    state.status.terminal.reason ||
      'The live terminal is Tailscale-only.',
    { icon: 'lock' }
  );
}

// Per-open chrome around the pane that doesn't depend on a terminal object.
function syncPaneChrome(session, isMirror) {
  // The composer (always docked) is phone-only — the
  // PC mirror already has a real keyboard with full predictive support.
  // Reset visible on every (non-mirror) open so a prior mirror open can't
  // leave it stuck hidden.
  els.terminalComposeBar.hidden = isMirror;

  // 🎤 dictation needs the voice-transcriber configured *and*
  // MediaRecorder support; 📷 screenshot OCR needs photo-ocr
  // configured. Re-synced per open because /api/status can land after the
  // composer mounted at boot — the mic renders disabled, the OCR option
  // hides inside the image menu (composer.js).
  terminalComposer.setAvailability({
    dictate: voiceDictationAvailable(),
    ocr: !!(state.status && state.status.screenshot_ocr),
  });

  // Which ⋮ read-aloud rows to offer lives in terminal-readaloud.js — it
  // needs both the cheap status flag and a live hub health probe (see
  // refreshReadAloudAvailability for why).
  refreshReadAloudAvailability();

  // Mirror window uses a uniquely identifiable OS title so the launcher
  // can find this Edge --app window via EnumWindows and dismiss it
  // with WM_CLOSE on Stop & Close.
  if (isMirror) announceMirrorWindow(session.session_id, sessionTitle(session));
}

// Re-show a warm terminal: LRU-touch it, repaint, and either reuse its
// live WS or reconnect.
function resumeCachedTerminal(sid, cached, tt) {
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
    updateLatestPill();
    cached.term.focus();
  } else {
    cached.retryCount = 0;
    cached.giveUpAt = 0;
    connectTerminalWs(cached);
  }
}

// Build the xterm instance (+ fit addon on the phone, + WebGL renderer) and
// open it into the host.
function openXterm(isMirror) {
  // Source the terminal colours from the design tokens so they can't fork
  // from the stylesheet. --term-bg/--term-fg are the
  // terminal-screen tokens, following the app theme
  // — resolved by termScreenTheme(), which also carries the
  // light ANSI palette.
  const term = new window.Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 13,
    scrollback: 10000,
    theme: termScreenTheme(),
    // Per-cell contrast floor for the light screen; 1 (off)
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
  return { term: term, fit: fit, webgl: loadWebgl(term) };
}

// GPU-accelerated renderer. Falls back to the default DOM renderer
// on any failure (no WebGL2, driver bug, OS reclaiming the context
// under memory pressure). Without the fallback the terminal would
// freeze; with it, worst case is same perf as before.
export function loadWebgl(term) {
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
  return webgl;
}

// Full-screen differential agents (Codex/Antigravity/Copilot ratatui)
// drive the pan-not-reflow keyboard path: on these the phone
// pans the fixed canvas above the keyboard rather than resizing the PTY.
// Resolved off the live /api/agents flag; an unknown/missing agent (or a
// degraded fallback agents list) reads as non-fullscreen — Claude's
// inline reflow, the safe default. Board drill-down and ?session=
// deep links pass a synthetic {session_id, name} with no agent field
// — resolve it from the sessions list so a Codex session opened
// from the Board still gets the fullscreen client handling.
function isFullscreenAgent(session, sid) {
  const liveSession = (state.sessions || []).find(function (x) {
    return x.session_id === sid;
  });
  const agentId = session.agent || (liveSession && liveSession.agent) || '';
  const knownAgent = (state.agents || []).find(function (a) {
    return a.id === agentId;
  });
  return !!(knownAgent && knownAgent.fullscreen);
}

// Register in the warm cache and evict the least-recently-used
// beyond the cap — a full teardown, so its WS/GL context are released.
function registerWarmTerminal(t) {
  _termCache.set(t.sid, t);
  while (_termCache.size > _TERM_CACHE_MAX) {
    const oldest = _termCache.values().next().value;
    if (!oldest || oldest === t) break;
    disposeTerminal(oldest);
  }
}

// Live-refresh the title while the overlay is open. The main
// sessions poll is paused under the overlay (main.js), so without this an
// open terminal / PC mirror window stays stuck on its first-paint title when
// the agent renames the conversation (or a first-prompt title lands). Reuses
// SESSIONS_POLL_MS so the session-fetch cadence is unchanged whether the
// overlay is open or closed — just a direct fetch with no Running-sessions
// list re-render, which is what the main poll's pause deliberately avoids.
function startTitleRefresh(t) {
  t.stopTitlePoll = startPoll(function () {
    if (t !== state.terminal) return;
    return jsonApi('/api/claude-code/sessions').then(function (body) {
      if (t !== state.terminal) return;
      const s = (body.sessions || []).find(function (x) {
        return x.session_id === t.sid;
      });
      if (s) refreshTerminalTitle(t, s);
    }).catch(function () { /* best-effort; title just won't refresh */ });
  }, SESSIONS_POLL_MS).stop;
}

// Full teardown of the ACTIVE terminal — kept for the paths where the
// session itself is over (mirror shutdown frame, stop-from-terminal): a
// warm cache entry would only be a zombie there.
export function closeTerminal() {
  const t = state.terminal;
  if (!t) return;
  disposeTerminal(t);
  // Release any keyboard-driven override so the next open
  // starts from the CSS-driven full height and inset:0 origin.
  releaseOverlayPin();
}

// iOS PWA rubber-band lets the user drag the whole body while the
// terminal overlay is open, tucking the terminal header under the
// status bar. Pin the body with position:fixed and stash the scroll
// position so we can restore it on close. Idempotent — re-opens from
// the sessions list re-enter through openTerminal but the body must
// stay pinned with the original scrollY.
let _savedScrollY = 0;

// The plain pin the vendored nav's README asks of an app that pins its body
// under an overlay (save scrollY, pin at -scrollY, restore on close). The
// nav's `html { min-height: calc(100dvh + 1px) }` keeps the document
// scrollable while the body is out of flow, so no installed-app branch.
function lockBodyScroll() {
  // Wide layout: the view docks beside the list, which scrolls on
  // its own; pinning the body would freeze the list it sits next to.
  if (isWideLayout()) return;
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

// The terminal half of closing the session overlay — session-overlay.js's
// closeSessionOverlay() (the ‹ Back button, stop-from-anywhere) calls this
// after it has closed the chat pane; nothing else should.
export function hideTerminal() {
  // Leaving the Coding tab silences any in-flight read-aloud — the
  // speech queue / hub audio is global and would otherwise keep talking
  // off-screen.
  stopReading();
  // Stash, don't dispose: the xterm keeps its painted frame and
  // the WS keeps streaming, so re-opening this session is instant and
  // never triggers the server's repaint nudge. The host's DOM is NOT
  // cleared for the same reason.
  stashActiveTerminal();
  terminalComposer.closePopovers();
  closeTerminalMenu();
  els.terminalOverlay.hidden = true;
  document.body.classList.remove('terminal-open');
  unlockBodyScroll();
  setTerminalStatus(null);
  fetchSessions().catch(function () {});
}

export function wireTerminal() {
  // The terminal screen follows the app theme: live-restyle
  // the open terminal whenever the app theme flips — xterm colors live in
  // the renderer, so CSS alone can't restyle an already-open screen;
  // options.theme can.
  new MutationObserver(applyTermTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });
  // User theme file — best-effort; the built-ins are already
  // a complete theme, so a missing/failed fetch changes nothing.
  jsonApi('/api/terminal-themes').then(function (body) {
    setUserTermThemes(body && body.themes);
    applyTermTheme();
  }).catch(function () { /* built-ins stand */ });
  // ‹ Back closes the whole overlay, whichever pane is showing.
  els.terminalBack.addEventListener('click', closeSessionOverlay);
  wireSessionModeToggle();
  // ⋮ session menu + the Latest pill — terminal-bar.js. Stop and
  // kill live in the menu, not as an in-bar ✕.
  wireTerminalMenu();
  mountTerminalComposer();
  watchHostSize();
  wireTerminalImage();
  // Read-aloud — wiring lives in
  // terminal-readaloud.js, alongside its ⋮ menu rows and the
  // summary modal it opens.
  wireReadAloud();
}
