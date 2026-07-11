/* PTY WebSocket lifecycle and reconnect policy for terminal.js.
 *
 * The active terminal object remains in shared state; this module owns the
 * socket handlers, terminal status affordance, visibility-aware backoff, and
 * passkey refresh needed to reconnect it.
 */

import { els, state } from './state.js';
import { apiFailToast, readToken } from './api.js';
import { clearTerminalToken, ensureTerminalToken } from './webauthn.js';

const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000];
const RECONNECT_GIVE_UP_MS = 30000;

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

export function setTerminalStatus(message) {
  if (!els.terminalStatus) return;
  if (message) {
    els.terminalStatus.textContent = message;
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
      setTerminalStatus('🔒 ' + (reason || 'Terminal is Tailscale-only') +
        ' — open the launcher over your Tailscale URL.');
      return;
    }
    if (event.code === 4404) {
      setTerminalStatus('Session not found — it may have ended.');
      return;
    }
    if (event.code === 4401) {
      clearTerminalToken();
      terminal.tt = '';
      setTapToReconnect(terminal, '🔒 ' + (reason || 'Passkey unlock required'));
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

function setTapToReconnect(terminal, label) {
  if (!terminal || terminal !== state.terminal || !els.terminalStatus) return;
  clearTerminalReconnect(terminal);
  setTerminalStatus(label || 'Tap to reconnect');
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
