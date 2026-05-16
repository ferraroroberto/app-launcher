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
import { jsonApi, readToken, toast } from './api.js';
import { fetchSessions } from './sessions.js';
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

  const ws = new WebSocket(termWsUrl(sid, tt));
  const t = { sid: sid, ws: ws, term: term, fit: fit, webgl: webgl, mirror: isMirror };
  state.terminal = t;

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
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'resize', rows: term.rows, cols: term.cols,
      }));
    }
  }

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

  ws.onopen = function () {
    setTerminalStatus(null);
    applySize();
    term.focus();
  };
  ws.onmessage = function (ev) {
    // tail -f follow: snap back to bottom on new output, but only
    // if the user was already there. If they scrolled up to read
    // history, leave them alone — they'll resume auto-follow by
    // scrolling back to the bottom themselves. The -1 fudge handles
    // iOS fractional touch-scroll states that would otherwise stick
    // the view one row above the tail forever.
    const b = term.buffer.active;
    const wasAtBottom = b.viewportY >= b.baseY - 1;
    term.write(ev.data, function () {
      if (wasAtBottom) {
        try { term.scrollToBottom(); } catch (_) {}
      }
    });
  };
  ws.onerror = function () { setTerminalStatus('Connection error.'); };
  ws.onclose = function (ev) {
    const reason = (ev && ev.reason) ? ev.reason : '';
    const m = ev.code === 4000 ? 'Session ended.'
      : ev.code === 4401 ? '🔒 ' + (reason || 'Passkey unlock required') +
          ' — re-open from Sessions.'
      : ev.code === 4403 ? '🔒 ' + (reason ||
          'Terminal is Tailscale-only') + ' — open the launcher over your ' +
          'Tailscale URL.'
      : ev.code === 4404 ? 'Session not found — it may have ended.'
      : ev.code === 4502 ? 'Session host unreachable on the PC.'
      : (reason || 'Disconnected.');
    setTerminalStatus(m);
    if (ev.code === 4401) clearTerminalToken();
  };

  term.onData(function (d) {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'input', data: d }));
    }
  });
}

export function closeTerminal() {
  const t = state.terminal;
  state.terminal = null;
  if (!t) return;
  if (t.sizeTimer) clearInterval(t.sizeTimer);
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

export function wireTerminal() {
  els.terminalBack.addEventListener('click', hideTerminal);
  els.terminalCtrlC.addEventListener('click', function () {
    const t = state.terminal;
    if (t && t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: '\x03' }));
    }
    if (t && t.term) t.term.focus();
  });
  els.terminalQuit.addEventListener('click', async function () {
    const t = state.terminal;
    if (!t) return;
    if (!confirm('Quit this Claude session?')) return;
    try {
      await jsonApi(
        '/api/claude-code/sessions/' + encodeURIComponent(t.sid) + '/stop',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: 'quit' }),
        }
      );
      toast('🛑 Quitting…', 'good');
    } catch (exc) {
      toast('Quit failed: ' + (exc.message || exc), 'error');
    }
  });
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
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return;
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
