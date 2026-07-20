/* PTY WebSocket lifecycle and reconnect policy for terminal.js.
 *
 * The active terminal object remains in shared state; this module owns the
 * socket handlers, terminal status affordance, visibility-aware backoff, and
 * passkey refresh needed to reconnect it.
 */

import { els, state } from './state.js';
import { apiFailToast, escapeHtml, readToken } from './api.js';
import { clearTerminalToken, ensureTerminalToken } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';

const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000];
const RECONNECT_GIVE_UP_MS = 30000;

// Repaint batching for full-screen differential agents (#430). Empirical
// probe: Codex/ratatui re-emits its ENTIRE transcript on every winsize
// change (~65 KB for a long conversation — same magnitude as a full
// `codex resume` attach). The session-host deliberately fires such a
// change on every (re)connect (the #128 width-toggle nudge), and a real
// rotation fires one too. Written chunk-by-chunk into xterm that storm
// renders as a visible scroll-through of the whole conversation. Instead,
// buffer everything that arrives right after a (re)connect / resize and
// write it in ONE term.write() — xterm renders once, landing directly on
// the final frame. The batch closes on the first quiet gap (the storm is
// a burst) or at a hard deadline, whichever comes first.
const REPAINT_BATCH_QUIET_MS = 700;
const REPAINT_BATCH_MAX_MS = 6000;

export function beginRepaintBatch(terminal) {
  if (!terminal || !terminal.isFullscreen) return;
  // Every caller (a fresh (re)connect's ws.onopen, or the first real size
  // sent on a connection) means "start a clean concealment window" — never
  // preserve a batch left over from a previous call. Without this, a
  // connection that dropped mid-batch (e.g. after the server's _CLEAR_FRAME
  // message but before its snapshot payload arrived) leaves batchBuf
  // partially filled AND its batchQuietTimer still armed; the next
  // reconnect's beginRepaintBatch only reset batchTimer, so that stale
  // timer could later fire on its own old schedule and flush whatever
  // happens to be in batchBuf at that moment — content from two different
  // connections mixed or clobbered together. Reported as a live-session
  // "conversation beginning visible, middle missing, latest lines visible"
  // corruption (issue #435 follow-up).
  if (terminal.batchQuietTimer) {
    clearTimeout(terminal.batchQuietTimer);
    terminal.batchQuietTimer = null;
  }
  terminal.batchBuf = [];
  terminal.batchDeadline = Date.now() + REPAINT_BATCH_MAX_MS;
  // Conceal the storm (#430 round 2): even flushed as ONE write, xterm
  // parses large writes in per-animation-frame slices, so the transcript
  // still visibly scrolls through. Hide the canvas for the batch window
  // and reveal on flush — the user lands directly on the final frame.
  try {
    if (terminal.term && terminal.term.element) {
      terminal.term.element.style.visibility = 'hidden';
    }
  } catch (_) { /* best effort */ }
  setTerminalStatus('Loading the current frame…', { icon: 'hourglass' });
  if (terminal.batchTimer) clearTimeout(terminal.batchTimer);
  terminal.batchTimer = setTimeout(function () {
    flushRepaintBatch(terminal);
  }, REPAINT_BATCH_MAX_MS);
}

function revealTerminal(terminal) {
  try {
    if (terminal.term && terminal.term.element) {
      terminal.term.element.style.visibility = '';
    }
  } catch (_) { /* best effort */ }
  // The status line is global chrome — only the ACTIVE terminal may clear
  // it (a stashed terminal's late flush must not wipe another's message).
  if (terminal === state.terminal &&
      els.terminalStatus && !els.terminalStatus.hidden &&
      els.terminalStatus.textContent.indexOf('Loading the current frame') !== -1) {
    setTerminalStatus(null);
  }
}

export function flushRepaintBatch(terminal) {
  if (!terminal) return;
  if (terminal.batchTimer) {
    clearTimeout(terminal.batchTimer);
    terminal.batchTimer = null;
  }
  if (terminal.batchQuietTimer) {
    clearTimeout(terminal.batchQuietTimer);
    terminal.batchQuietTimer = null;
  }
  const buf = terminal.batchBuf;
  terminal.batchBuf = null;
  terminal.batchDeadline = 0;
  // A stashed (hidden but warm, #430) terminal still gets its batch
  // written — its buffer must stay current for the instant re-open. Only
  // a torn-down terminal is off-limits; disposeTerminal clears the batch
  // timers before term.dispose(), so this can't fire post-dispose. The
  // element check is belt-and-braces for a not-yet-opened xterm.
  if (!terminal.term || !terminal.term.element) return;
  if (!buf || !buf.length) {
    revealTerminal(terminal);
    return;
  }
  terminal.term.write(buf.join(''), function () {
    try { terminal.term.scrollToBottom(); } catch (_) { /* best effort */ }
    revealTerminal(terminal);
  });
}

function termWsUrl(sid, terminalToken) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const params = new URLSearchParams();
  const bearerToken = readToken();
  if (bearerToken) params.set('token', bearerToken);
  if (terminalToken) params.set('tt', terminalToken);
  const query = params.toString();
  return proto + '//' + location.host + '/api/claude-code/sessions/' +
    encodeURIComponent(sid) + '/ws' + (query ? '?' + query : '');
}

// opts.icon (a Lucide glyph name) renders a leading icon before the escaped
// message; without it the status stays plain text as before.
export function setTerminalStatus(message, opts) {
  if (!els.terminalStatus) return;
  if (message) {
    const iconName = opts && opts.icon;
    if (iconName) {
      els.terminalStatus.innerHTML = icon(iconName) + ' ' + escapeHtml(message);
    } else {
      els.terminalStatus.textContent = message;
    }
    els.terminalStatus.hidden = false;
  } else {
    els.terminalStatus.hidden = true;
  }
}

function isShutdownFrame(data) {
  if (typeof data !== 'string' || data.charCodeAt(0) !== 0x7b) return false;
  try {
    const message = JSON.parse(data);
    return !!message && typeof message === 'object' && message.type === 'shutdown';
  } catch (_) {
    return false;
  }
}

export function routeFrame(data, isMirror) {
  if (isShutdownFrame(data)) return isMirror ? 'close-mirror' : 'swallow';
  return 'write';
}

export function connectTerminalWs(terminal) {
  if (terminal.ws) {
    try {
      terminal.ws.onopen = null;
      terminal.ws.onmessage = null;
      terminal.ws.onerror = null;
      terminal.ws.onclose = null;
    } catch (_) { /* dying socket; replacement continues */ }
  }
  const ws = new WebSocket(termWsUrl(terminal.sid, terminal.tt));
  terminal.ws = ws;

  ws.onopen = function () {
    if (terminal !== state.terminal) return;
    terminal.retryCount = 0;
    terminal.giveUpAt = 0;
    clearTerminalReconnect(terminal);
    setTerminalStatus(null);
    if (terminal.isFullscreen && terminal.term) {
      try { terminal.term.clear(); } catch (_) { /* best effort */ }
      // The session-host answers every fullscreen (re)connect with a
      // clear-frame + a headless-VT snapshot (bounded scrollback history
      // + current frame, #432/#435) sent as a couple of WS messages —
      // batch them into a single paint so xterm doesn't visibly crawl in.
      beginRepaintBatch(terminal);
    }
    if (terminal.applySize) terminal.applySize();
    if (terminal.term) terminal.term.focus();
  };
  ws.onmessage = function (event) {
    const route = routeFrame(event.data, terminal.mirror);
    if (route === 'close-mirror') {
      if (terminal.onShutdown) terminal.onShutdown();
      try { window.close(); } catch (_) { /* teardown still stands */ }
      return;
    }
    if (route === 'swallow' || !terminal.term) return;
    // #499: stamp every PTY output frame so the compose bar's bulk-send
    // settle watch (terminal-compose.js) can tell "the agent echoed the
    // paste and went quiet" from "the paste is still being ingested".
    terminal.lastOutputAt = Date.now();
    // Active repaint batch (#430): buffer the burst, flush on the first
    // quiet gap or at the hard deadline — one write, one render.
    if (terminal.batchBuf) {
      terminal.batchBuf.push(event.data);
      if (terminal.batchQuietTimer) clearTimeout(terminal.batchQuietTimer);
      if (Date.now() >= terminal.batchDeadline) {
        flushRepaintBatch(terminal);
      } else {
        terminal.batchQuietTimer = setTimeout(function () {
          flushRepaintBatch(terminal);
        }, REPAINT_BATCH_QUIET_MS);
      }
      return;
    }
    const buffer = terminal.term.buffer.active;
    const wasAtBottom = buffer.viewportY >= buffer.baseY - 1;
    terminal.term.write(event.data, function () {
      if (wasAtBottom) {
        try { terminal.term.scrollToBottom(); } catch (_) { /* best effort */ }
      }
    });
  };
  ws.onerror = function () { /* onclose drives UI */ };
  ws.onclose = function (event) {
    if (terminal !== state.terminal) return;
    const reason = event && event.reason ? event.reason : '';
    if (event.code === 4000) { setTerminalStatus('Session ended.'); return; }
    if (event.code === 4403) {
      setTerminalStatus((reason || 'Terminal is Tailscale-only') +
        ' — open the launcher over your Tailscale URL.', { icon: 'lock' });
      return;
    }
    if (event.code === 4404) {
      setTerminalStatus('Session not found — it may have ended.');
      return;
    }
    if (event.code === 4401) {
      clearTerminalToken();
      terminal.tt = '';
      setTapToReconnect(terminal, reason || 'Passkey unlock required', { icon: 'lock' });
      return;
    }
    if (!terminal.giveUpAt) {
      terminal.giveUpAt = Date.now() + RECONNECT_GIVE_UP_MS;
    }
    scheduleReconnect(terminal);
  };
}

function scheduleReconnect(terminal) {
  if (!terminal || terminal !== state.terminal || terminal.retryTimer) return;
  if (Date.now() >= terminal.giveUpAt) {
    setTapToReconnect(terminal, 'Tap to reconnect');
    return;
  }
  if (document.visibilityState !== 'visible') {
    setTerminalStatus('Reconnecting when visible…');
    if (!terminal.visibilityListener) {
      terminal.visibilityListener = function () {
        if (document.visibilityState !== 'visible') return;
        document.removeEventListener('visibilitychange', terminal.visibilityListener);
        terminal.visibilityListener = null;
        terminal.retryCount = 0;
        terminal.giveUpAt = Date.now() + RECONNECT_GIVE_UP_MS;
        scheduleReconnect(terminal);
      };
      document.addEventListener('visibilitychange', terminal.visibilityListener);
    }
    return;
  }
  const index = Math.min(
    terminal.retryCount || 0,
    RECONNECT_DELAYS_MS.length - 1
  );
  const delay = RECONNECT_DELAYS_MS[index];
  terminal.retryCount = (terminal.retryCount || 0) + 1;
  setTerminalStatus('Reconnecting…');
  terminal.retryTimer = setTimeout(function () {
    terminal.retryTimer = null;
    if (terminal === state.terminal) connectTerminalWs(terminal);
  }, delay);
}

function setTapToReconnect(terminal, label, opts) {
  if (!terminal || terminal !== state.terminal || !els.terminalStatus) return;
  clearTerminalReconnect(terminal);
  setTerminalStatus(label || 'Tap to reconnect', opts);
  els.terminalStatus.style.cursor = 'pointer';
  els.terminalStatus.style.textDecoration = 'underline';
  terminal.tapHandler = function () {
    if (terminal !== state.terminal) return;
    els.terminalStatus.removeEventListener('click', terminal.tapHandler);
    terminal.tapHandler = null;
    els.terminalStatus.style.cursor = '';
    els.terminalStatus.style.textDecoration = '';
    terminal.retryCount = 0;
    terminal.giveUpAt = Date.now() + RECONNECT_GIVE_UP_MS;
    setTerminalStatus('Connecting…');
    ensureTerminalToken().then(function (terminalToken) {
      if (terminal !== state.terminal) return;
      terminal.tt = terminalToken;
      connectTerminalWs(terminal);
    }).catch(function (error) {
      apiFailToast('Passkey unlock failed', error);
      setTapToReconnect(terminal, 'Tap to reconnect');
    });
  };
  els.terminalStatus.addEventListener('click', terminal.tapHandler);
}

export function clearTerminalReconnect(terminal) {
  if (!terminal) return;
  // Drop any pending repaint batch without writing it (#430) — this runs
  // on teardown and right before a fresh connect, where the next onopen
  // starts a new batch against a cleared screen anyway. Un-hide the
  // canvas so a give-up ("Tap to reconnect") never strands it invisible.
  if (terminal.batchTimer) {
    clearTimeout(terminal.batchTimer);
    terminal.batchTimer = null;
  }
  if (terminal.batchQuietTimer) {
    clearTimeout(terminal.batchQuietTimer);
    terminal.batchQuietTimer = null;
  }
  terminal.batchBuf = null;
  terminal.batchDeadline = 0;
  try {
    if (terminal.term && terminal.term.element) {
      terminal.term.element.style.visibility = '';
    }
  } catch (_) { /* best effort */ }
  if (terminal.retryTimer) {
    clearTimeout(terminal.retryTimer);
    terminal.retryTimer = null;
  }
  if (terminal.visibilityListener) {
    document.removeEventListener('visibilitychange', terminal.visibilityListener);
    terminal.visibilityListener = null;
  }
  if (terminal.tapHandler && els.terminalStatus) {
    els.terminalStatus.removeEventListener('click', terminal.tapHandler);
    terminal.tapHandler = null;
    els.terminalStatus.style.cursor = '';
    els.terminalStatus.style.textDecoration = '';
  }
}
