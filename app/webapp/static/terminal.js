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
 */

import { els, state } from './state.js';
import { readToken, toast } from './api.js';
import { fetchSessions } from './sessions.js';
import { enableNativeTouchScroll } from './terminal-touch.js';
import {
  clearTerminalToken,
  ensureTerminalToken,
  readTerminalToken,
} from './webauthn.js';

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
    if (t.applySize) t.applySize();
    if (t.term) t.term.focus();
  };
  ws.onmessage = function (ev) {
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
      toast('Passkey unlock failed: ' + (exc.message || exc), 'error');
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
    els.terminalTitle.textContent = (session.live_title && session.live_title.trim()) ? session.live_title : (session.name || 'session');
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
    toast('Passkey unlock failed: ' + (exc.message || exc), 'error');
    return;
  }
  closeTerminal();
  els.terminalOverlay.hidden = false;
  document.body.classList.add('terminal-open');
  lockBodyScroll();
  els.terminalTitle.textContent = (session.live_title && session.live_title.trim()) ? session.live_title : (session.name || 'session');
  setTerminalStatus('Connecting…');

  // The PC mirror window connects over loopback. It renders whatever
  // size the phone set and never resizes the PTY — the phone is the
  // single size authority, so the two clients never fight (the server
  // also ignores resize frames from role=pc).
  const isMirror = !!(state.status && state.status.terminal &&
    state.status.terminal.reason === 'loopback');

  // The compose bar (issue #37) is phone-only — the PC mirror already
  // has a real keyboard with full predictive support. Reset the button
  // visible on every (non-mirror) open so a prior mirror open can't
  // leave it stuck hidden.
  els.terminalCompose.hidden = isMirror;

  // Mirror window uses a uniquely identifiable OS title so the launcher
  // can find this Edge --app window via EnumWindows and dismiss it
  // with WM_CLOSE on Stop & Close (issue #20). Must run on every open
  // because Edge sets the title from the page after load. The
  // console.info is intentional — open DevTools on the mirror window
  // to confirm the title was actually applied if Stop & Close fails.
  if (isMirror) {
    const mirrorTitle = 'app-launcher-mirror-' + sid;
    document.title = mirrorTitle;
    console.info('[app-launcher] mirror title set:', mirrorTitle);
  }

  const term = new window.Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 13,
    scrollback: 10000,
    theme: { background: '#0a0a0a', foreground: '#e6e6e6' },
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

  const t = {
    sid: sid, ws: null, tt: tt, term: term, fit: fit, webgl: webgl,
    mirror: isMirror, retryCount: 0, giveUpAt: 0,
    retryTimer: null, visibilityListener: null, tapHandler: null,
    disposeTouch: null, composeOpen: false,
  };
  state.terminal = t;

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
    try { if (fit) fit.fit(); } catch (_) {}
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
  if (t.disposeTouch) { try { t.disposeTouch(); } catch (_) {} }
  if (t.onWindowResize) window.removeEventListener('resize', t.onWindowResize);
  if (t.onVisualViewport && window.visualViewport) {
    window.visualViewport.removeEventListener('resize', t.onVisualViewport);
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
  closeTerminal();
  closeKeysPopover();
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
  const fd = new FormData();
  fd.append('file', file, file.name || 'image.png');
  try {
    const headers = new Headers();
    const bt = readToken();
    if (bt) headers.set('Authorization', 'Bearer ' + bt);
    const tt = readTerminalToken();
    if (tt) headers.set('X-Terminal-Token', tt);
    const res = await fetch(
      '/api/claude-code/sessions/' + encodeURIComponent(t.sid) + '/image',
      { method: 'POST', headers: headers, body: fd }
    );
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    toast('🖼️ Image sent — its path was pasted into the prompt.', 'good');
    if (t.term) t.term.focus();
  } catch (exc) {
    toast('Image failed: ' + (exc.message || exc), 'error');
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

let _keysOutsideHandler = null;

function closeKeysPopover() {
  if (!els.terminalKeysPopover) return;
  els.terminalKeysPopover.hidden = true;
  if (_keysOutsideHandler) {
    document.removeEventListener('pointerdown', _keysOutsideHandler);
    _keysOutsideHandler = null;
  }
}

function openKeysPopover() {
  if (!els.terminalKeysPopover) return;
  els.terminalKeysPopover.hidden = false;
  // Close on any tap outside the popover or its toggle button. The
  // opening tap's pointerdown has already fired by the time this click
  // handler runs, so binding now won't catch it; the contains() guards
  // cover a tap on the ⌨️ button itself.
  if (!_keysOutsideHandler) {
    _keysOutsideHandler = function (ev) {
      if (els.terminalKeysPopover.contains(ev.target) ||
          els.terminalKeys.contains(ev.target)) return;
      closeKeysPopover();
    };
    document.addEventListener('pointerdown', _keysOutsideHandler);
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
    const bytes = KEY_BYTES[btn.getAttribute('data-key')];
    if (!bytes) return;
    const t = state.terminal;
    if (t && t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: bytes }));
    }
    if (t && t.term) t.term.focus();
    if (bytes === '\r' || bytes === '\x1b') closeKeysPopover();
  });
}

// Compose bar (issue #37): a normal <textarea> with default predictive/
// autocorrect/spellcheck so iOS/Android keyboards offer suggestions —
// which they can't inside xterm's per-keystroke-wiped helper textarea.
// ➤ Send forwards the buffered text + \r to the PTY in one WS frame.

function growComposeInput() {
  // Auto-grow up to ~4 rows; the iOS return key adds newlines, only
  // ➤ Send forwards to the PTY.
  const ta = els.terminalComposeInput;
  ta.style.height = 'auto';
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height = Math.min(ta.scrollHeight, 4 * lineHeight + 16) + 'px';
}

function resetComposeBar() {
  els.terminalComposeBar.hidden = true;
  els.terminalComposeInput.value = '';
  els.terminalComposeInput.style.height = '';
}

function setComposeOpen(open) {
  const t = state.terminal;
  if (!t) return;
  t.composeOpen = open;
  els.terminalComposeBar.hidden = !open;
  if (open) {
    // Focusing the textarea pops the phone keyboard with predictive on.
    els.terminalComposeInput.focus();
  } else if (t.term) {
    // Direct mode resumes — hand focus back to xterm.
    t.term.focus();
  }
}

function wireCompose() {
  els.terminalCompose.addEventListener('click', function () {
    const t = state.terminal;
    if (!t) return;
    setComposeOpen(!t.composeOpen);
  });
  els.terminalComposeSend.addEventListener('click', function () {
    const t = state.terminal;
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    const text = els.terminalComposeInput.value;
    if (!text) return;
    t.ws.send(JSON.stringify({ type: 'input', data: text + '\r' }));
    els.terminalComposeInput.value = '';
    els.terminalComposeInput.style.height = '';
    els.terminalComposeInput.focus();
  });
  els.terminalComposeInput.addEventListener('input', growComposeInput);
}

export function wireTerminal() {
  els.terminalBack.addEventListener('click', hideTerminal);
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
      t.ws.send(JSON.stringify({ type: 'input', data: text }));
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
