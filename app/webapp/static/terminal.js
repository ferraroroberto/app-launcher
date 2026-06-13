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
import { fetchSessions, sessionTitle } from './sessions.js';
import { enableNativeTouchScroll } from './terminal-touch.js';
import {
  cancelHub,
  cancelSpeech,
  extractLastReply,
  isHubAvailable,
  isSpeaking,
  isSpeechSupported,
  onSpeakingChange,
  onSpeechEnd,
  probeHub,
  speak,
  speakHub,
} from './terminal-readback.js';
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
    toast('Passkey unlock failed: ' + (exc.message || exc), 'error');
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

  // The 🔊 read-aloud button (issue #190) reads the last reply aloud through
  // one of two voices: the hub's Orpheus voice (#203) when the local-llm-hub
  // is configured, else the on-device Web Speech voice. Show it when EITHER
  // path is possible. `state.status.tts` is the cheap config-presence flag; a
  // live /api/tts/health probe then refines which path the click takes.
  const ttsConfigured = !!(state.status && state.status.tts);
  els.terminalSpeak.hidden = !(isSpeechSupported() || ttsConfigured);
  if (ttsConfigured) {
    probeHub({
      token: readToken(),
      terminalToken: readTerminalToken(),
    }).then(function (ok) {
      // A reachable hub means the button is useful even where Web Speech
      // isn't supported (e.g. some embedded WebViews).
      if (ok) els.terminalSpeak.hidden = false;
    });
  }

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
    if (vp && els.terminalOverlay) {
      const h = keyboardOverlayHeight(window.innerHeight, vp.height);
      if (h != null) {
        els.terminalOverlay.style.height = h + 'px';
        els.terminalOverlay.style.bottom = 'auto';
        els.terminalOverlay.style.top = Math.round(vp.offsetTop || 0) + 'px';
      } else {
        els.terminalOverlay.style.height = '';
        els.terminalOverlay.style.bottom = '';
        els.terminalOverlay.style.top = '';
      }
    }
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
    const headers = new Headers();
    const bt = readToken();
    if (bt) headers.set('Authorization', 'Bearer ' + bt);
    const tt = readTerminalToken();
    if (tt) headers.set('X-Terminal-Token', tt);
    const res = await fetch(
      '/api/claude-code/sessions/' + encodeURIComponent(t.sid) + '/image' +
        (inline ? '?inline=1' : ''),
      { method: 'POST', headers: headers, body: fd }
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

let _keysOutsideHandler = null;
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

// Compose bar (issue #37): a normal <textarea> with default predictive/
// autocorrect/spellcheck so iOS/Android keyboards offer suggestions —
// which they can't inside xterm's per-keystroke-wiped helper textarea.
// ➤ Send forwards the buffered text, then a submitting \r as a SEPARATE
// WS frame (see sendSubmit / #166).

// Max visible rows before the textarea scrolls internally. Roomy enough
// for a long dictated voice note (#165) without the bar eating the whole
// screen when the keyboard is up. The CSS min-height floors it at 2 rows.
const _COMPOSE_MAX_ROWS = 8;

function growComposeInput() {
  // Auto-grow up to _COMPOSE_MAX_ROWS; the iOS return key adds newlines,
  // only ➤ Send forwards to the PTY.
  const ta = els.terminalComposeInput;
  ta.style.height = 'auto';
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height =
    Math.min(ta.scrollHeight, _COMPOSE_MAX_ROWS * lineHeight + 16) + 'px';
  // Keep the caret (end of a freshly inserted transcript) in view.
  ta.scrollTop = ta.scrollHeight;
}

function resetComposeBar() {
  els.terminalComposeBar.hidden = true;
  els.terminalComposeInput.value = '';
  els.terminalComposeInput.style.height = '';
  clearOcrStaging();
}

function setComposeOpen(open) {
  const t = state.terminal;
  if (!t) return;
  t.composeOpen = open;
  els.terminalComposeBar.hidden = !open;
  if (!open) {
    // Closing the bar abandons any in-flight recording so the mic isn't
    // left live behind a hidden bar, and drops any staged OCR images.
    stopRecording();
    clearOcrStaging();
  }
  if (open) {
    // Focusing the textarea pops the phone keyboard with predictive on.
    els.terminalComposeInput.focus();
  } else if (t.term) {
    // Direct mode resumes — hand focus back to xterm.
    t.term.focus();
  }
}

// Wrap a clipboard / compose payload in bracketed-paste markers (DECSET
// 2004) when the agent's TUI has them enabled, so it buffers the whole
// block as one atomic paste instead of absorbing a per-keystroke burst —
// which the Windows console input queue silently drops spans of under a
// multi-KB load (#64). This is exactly what xterm already does for its own
// native paste (term.onData); the 📋 button and compose ➤ Send bypass
// xterm, so they have to replicate it. Only bracket when the app actually
// asked for it (`term.modes.bracketedPasteMode`) — otherwise the literal
// `\x1b[200~` would land as garbage in an agent that doesn't grok it.
//
// Framing only — this never appends the submitting carriage return. A
// submit goes through `sendSubmit`, which delivers the CR as its OWN WS
// frame after this block (see #166).
export function framePaste(t, text) {
  const bracketed = !!(t.term && t.term.modes && t.term.modes.bracketedPasteMode);
  if (!bracketed) return text;
  return '\x1b[200~' + text + '\x1b[201~';
}

// Send a composed prompt to the PTY and submit it. The submitting carriage
// return is sent as its OWN WS frame *after* the (possibly bracketed) text
// block — never concatenated onto it.
//
// Why split: the webapp proxies each WS `input` frame to the session-host
// as a distinct `pty.write()`, so two frames become two PTY writes. That
// guarantees the `\x1b[201~` paste-end marker is written — and the TUI has
// finished exiting bracketed-paste mode — before the bare CR arrives. When
// the CR rode in the same frame as the end marker, the TUI intermittently
// absorbed it into paste finalization instead of running the prompt: the
// "➤ Send sometimes does nothing" race of #166. A CR *inside* the markers
// is literal pasted text by design, so the split is the only ordering that
// reliably submits. With bracketed mode off there is no paste state machine
// to race, but the two-frame path is harmless there, so it stays uniform.
export function sendSubmit(t, text) {
  if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
  t.ws.send(JSON.stringify({ type: 'input', data: framePaste(t, text) }));
  t.ws.send(JSON.stringify({ type: 'input', data: '\r' }));
}

// Voice dictation (issues #165 / #168): the 🎤 button in the compose bar
// records the mic and drops the transcript into the compose textarea for
// review — never straight into the PTY. Tap to start, tap to stop.
// Phone-only by virtue of living in the compose bar, which is hidden in
// the PC mirror window.
//
// Preferred flow (#168) is *streamed*: create a voice session, POST audio
// chunks at a 1 s cadence, and subscribe to a Server-Sent-Events stream of
// rolling `partial` transcripts that revise the dictated span live as you
// speak; `finish` settles the canonical text on stop. If streaming setup
// fails we fall back to the #165 single-shot path (buffer the whole take,
// POST it once to /api/transcribe) so dictation degrades rather than
// breaks. The `finish` call is the source of truth either way, so even a
// dead SSE stream still yields the full transcript on stop.
const _CHUNK_MS = 1000;
let _recorder = null;
let _recordChunks = [];
// Streaming state (#168). _voiceSession is the upstream session id;
// _streaming flips true only once a session is created. The chunk queue
// drains sequentially so chunks reach the session-host in order.
let _voiceSession = null;
let _streaming = false;
let _voiceEvents = null;
let _chunkQueue = [];
let _chunkDraining = false;
// The dictated span inside the textarea: [_dictStart, _dictStart+_dictLen].
// Each partial replaces exactly that span, preserving text typed before it.
let _dictStart = 0;
let _dictLen = 0;

// First supported of the recorder MIME ladder — iOS Safari usually only
// offers audio/mp4, everyone else audio/webm/opus. The voice-transcriber
// sniffs the real container at transcode time, so a truthful label is all
// that matters.
function pickAudioMime() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4;codecs=mp4a.40.2',
    'audio/mp4',
  ];
  const MR = window.MediaRecorder;
  if (!MR || !MR.isTypeSupported) return '';
  for (let i = 0; i < candidates.length; i++) {
    if (MR.isTypeSupported(candidates[i])) return candidates[i];
  }
  return '';
}

function setRecordingUI(on) {
  const btn = els.terminalRecord;
  if (!btn) return;
  btn.classList.toggle('recording', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.textContent = on ? '⏹' : '🎤';
  btn.title = on ? 'Stop recording' : 'Dictate (voice → text)';
}

// Auth headers for the transcribe endpoints (bearer + passkey terminal
// token), mirroring sendImage.
function voiceHeaders() {
  const headers = new Headers();
  const bt = readToken();
  if (bt) headers.set('Authorization', 'Bearer ' + bt);
  const tt = readTerminalToken();
  if (tt) headers.set('X-Terminal-Token', tt);
  return headers;
}

// EventSource can't set headers, so the SSE stream carries auth in the
// query string (the gates read query params).
function voiceQuery() {
  const params = new URLSearchParams();
  const bt = readToken();
  if (bt) params.set('token', bt);
  const tt = readTerminalToken();
  if (tt) params.set('tt', tt);
  const q = params.toString();
  return q ? '?' + q : '';
}

// Replace the tracked dictation span with the latest transcript, leaving
// any text the user typed before the span untouched, and keep the caret /
// end in view.
function renderDictation(text) {
  const ta = els.terminalComposeInput;
  ta.setRangeText(text, _dictStart, _dictStart + _dictLen, 'end');
  _dictLen = text.length;
  growComposeInput();
}

function closeVoiceEvents() {
  if (_voiceEvents) {
    try { _voiceEvents.close(); } catch (_) { /* best effort */ }
    _voiceEvents = null;
  }
}

// Sequentially POST queued audio chunks so they reach the session-host in
// order (overlapping POSTs could interleave on the raw file).
async function drainChunks() {
  if (_chunkDraining) return;
  _chunkDraining = true;
  try {
    while (_chunkQueue.length && _voiceSession) {
      const blob = _chunkQueue.shift();
      try {
        await fetch(
          '/api/transcribe/sessions/' + encodeURIComponent(_voiceSession) +
            '/chunk',
          { method: 'POST', headers: voiceHeaders(), body: blob }
        );
      } catch (_) { /* a dropped chunk is recoverable; finish reconciles */ }
    }
  } finally {
    _chunkDraining = false;
  }
}

async function startRecording() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
      !window.MediaRecorder) {
    toast('Recording not supported on this browser', 'error');
    return;
  }
  // Starting to talk silences any in-flight read-aloud (issue #190) — you're
  // answering, not still listening.
  stopReading();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (exc) {
    toast('Microphone unavailable: ' + (exc.message || exc), 'error');
    return;
  }
  const mime = pickAudioMime();
  try {
    _recorder = mime
      ? new MediaRecorder(stream, { mimeType: mime })
      : new MediaRecorder(stream);
  } catch (exc) {
    stream.getTracks().forEach(function (tr) { tr.stop(); });
    toast('Recorder failed: ' + (exc.message || exc), 'error');
    return;
  }
  _recordChunks = [];
  _chunkQueue = [];
  _voiceSession = null;
  _streaming = false;

  // Try to open a streamed session (#168). On any failure, fall back to
  // the buffered single-shot path (#165) — _streaming stays false.
  try {
    const res = await fetch('/api/transcribe/sessions', {
      method: 'POST', headers: voiceHeaders(),
    });
    if (res.ok) {
      const body = await res.json().catch(function () { return null; });
      if (body && body.session_id) {
        _voiceSession = body.session_id;
        _streaming = true;
      }
    }
  } catch (_) { /* fall back to buffered */ }

  if (_streaming) {
    // Anchor the dictation span at the caret (after a separator space when
    // the textarea already has trailing content), then stream partials in.
    const ta = els.terminalComposeInput;
    const before = ta.value.slice(0, ta.selectionStart);
    const sep = (before && !/\s$/.test(before)) ? ' ' : '';
    ta.setRangeText(sep, ta.selectionStart, ta.selectionEnd, 'end');
    _dictStart = ta.selectionStart;
    _dictLen = 0;
    try {
      _voiceEvents = new EventSource(
        '/api/transcribe/sessions/' + encodeURIComponent(_voiceSession) +
          '/events' + voiceQuery()
      );
      _voiceEvents.addEventListener('partial', function (ev) {
        try {
          const data = JSON.parse(ev.data);
          if (typeof data.transcript === 'string') renderDictation(data.transcript);
        } catch (_) { /* ignore a malformed frame */ }
      });
      // `final` also arrives via /finish's return value; closing here is
      // harmless — finish() is the source of truth.
      _voiceEvents.addEventListener('final', closeVoiceEvents);
    } catch (_) { _voiceEvents = null; }
  }

  _recorder.addEventListener('dataavailable', function (ev) {
    if (!ev.data || !ev.data.size) return;
    if (_streaming) {
      _chunkQueue.push(ev.data);
      drainChunks();
    } else {
      _recordChunks.push(ev.data);
    }
  });
  _recorder.addEventListener('stop', function () {
    stream.getTracks().forEach(function (tr) { tr.stop(); });
    setRecordingUI(false);
    if (_streaming) {
      finishStreaming();
    } else {
      const type = _recorder ? _recorder.mimeType : (mime || 'audio/webm');
      const blob = new Blob(_recordChunks, { type: type });
      _recordChunks = [];
      _recorder = null;
      if (blob.size) sendBufferedRecording(blob);
    }
  });
  // Timeslice only matters when streaming — it forces periodic
  // dataavailable so chunks flow during the take.
  _recorder.start(_streaming ? _CHUNK_MS : undefined);
  setRecordingUI(true);
}

function stopRecording() {
  if (_recorder && _recorder.state !== 'inactive') {
    try { _recorder.stop(); } catch (_) { /* stop fires anyway */ }
  } else {
    setRecordingUI(false);
  }
}

// Streamed stop (#168): flush remaining chunks, ask the voice-transcriber
// for the canonical transcript, settle the dictated span, tear down.
async function finishStreaming() {
  const sid = _voiceSession;
  _recorder = null;
  els.terminalRecord.disabled = true;
  const stopTimer = startWorkTimer(els.terminalRecord, '🎤');
  try {
    await drainChunks();
    const res = await fetch(
      '/api/transcribe/sessions/' + encodeURIComponent(sid) + '/finish',
      { method: 'POST', headers: voiceHeaders() }
    );
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    const body = await res.json().catch(function () { return null; });
    if (body && body.silent) {
      // Nothing heard — drop the empty span we anchored.
      renderDictation('');
      toast('🎤 Nothing heard — silent recording');
    } else if (body && typeof body.transcript === 'string') {
      renderDictation(body.transcript);
      toast('🎤 Transcribed — review, then ➤ Send.', 'good');
    }
    const ta = els.terminalComposeInput;
    ta.focus();
  } catch (exc) {
    toast('Transcription failed: ' + (exc.message || exc), 'error');
  } finally {
    closeVoiceEvents();
    _voiceSession = null;
    _streaming = false;
    stopTimer();
    els.terminalRecord.disabled = false;
  }
}

// Single-shot fallback (#165): the whole take in one POST to /api/transcribe.
async function sendBufferedRecording(blob) {
  const ext = (blob.type && blob.type.indexOf('mp4') >= 0) ? 'mp4' : 'webm';
  const fd = new FormData();
  fd.append('file', blob, 'recording.' + ext);
  els.terminalRecord.disabled = true;
  const stopTimer = startWorkTimer(els.terminalRecord, '🎤');
  try {
    const res = await fetch('/api/transcribe', {
      method: 'POST', headers: voiceHeaders(), body: fd,
    });
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    const body = await res.json().catch(function () { return null; });
    const text = body && body.transcript;
    if (body && body.silent) {
      toast('🎤 Nothing heard — silent recording');
      return;
    }
    if (!text) {
      toast('🎤 No transcript returned');
      return;
    }
    // Insert at the caret with a leading space when the textarea already
    // has trailing content, so dictation appends cleanly to typed text.
    const ta = els.terminalComposeInput;
    const before = ta.value.slice(0, ta.selectionStart);
    const sep = (before && !/\s$/.test(before)) ? ' ' : '';
    ta.setRangeText(sep + text, ta.selectionStart, ta.selectionEnd, 'end');
    growComposeInput();
    ta.focus();
    toast('🎤 Transcribed — review, then ➤ Send.', 'good');
  } catch (exc) {
    toast('Transcription failed: ' + (exc.message || exc), 'error');
  } finally {
    stopTimer();
    els.terminalRecord.disabled = false;
  }
}

// Tiny "working" indicator: swap a button's label for a ticking
// elapsed-seconds timer so a blind background wait — OCR, single-shot
// transcribe, streamed finish — visibly shows progress instead of looking
// stuck. ``workingLabel`` defaults to the hourglass glyph; pass a richer
// label for wide buttons. Returns a stop() that restores ``restoreText``.
function startWorkTimer(btn, restoreText, workingLabel) {
  const lbl = workingLabel || '⏳';
  const t0 = Date.now();
  btn.classList.add('working');
  function tick() {
    const s = Math.floor((Date.now() - t0) / 1000);
    btn.textContent = lbl + s + 's';
  }
  tick();
  const id = setInterval(tick, 500);
  return function stop() {
    clearInterval(id);
    btn.classList.remove('working');
    btn.textContent = restoreText;
  };
}

// Screenshot OCR (issue #171). The 📷 button *stages* one or more
// screenshots into a tray; nothing is sent yet. Each tap accumulates more
// images. When the user taps **Extract**, ALL staged images go to photo-ocr
// in a SINGLE /api/ocr call, so photo-ocr collates them into one
// deduplicated text (overlapping shots of one document are merged, duplicate
// boundary lines removed) — instead of one isolated OCR per image. The text
// drops into the compose textarea for review before ➤ Send. Model/prompt are
// left to photo-ocr's own config.
let _ocrStaged = [];        // File objects awaiting a collated extraction
let _ocrThumbUrls = [];     // object URLs to revoke when the tray clears

function clearOcrStaging() {
  _ocrStaged = [];
  _ocrThumbUrls.forEach(function (u) { URL.revokeObjectURL(u); });
  _ocrThumbUrls = [];
  renderOcrTray();
}

function renderOcrTray() {
  const strip = els.terminalOcrThumbs;
  strip.innerHTML = '';
  _ocrThumbUrls.forEach(function (u) { URL.revokeObjectURL(u); });
  _ocrThumbUrls = [];
  if (!_ocrStaged.length) {
    els.terminalOcrTray.hidden = true;
    return;
  }
  els.terminalOcrTray.hidden = false;
  _ocrStaged.forEach(function (file, idx) {
    const cell = document.createElement('div');
    cell.className = 'ocr-thumb';
    const img = document.createElement('img');
    const url = URL.createObjectURL(file);
    _ocrThumbUrls.push(url);
    img.src = url;
    img.alt = 'staged screenshot ' + (idx + 1);
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'ocr-thumb-x';
    rm.textContent = '✕';
    rm.title = 'Remove';
    rm.addEventListener('click', function () {
      _ocrStaged.splice(idx, 1);
      renderOcrTray();
    });
    cell.appendChild(img);
    cell.appendChild(rm);
    strip.appendChild(cell);
  });
  els.terminalOcrExtract.textContent =
    '📷 Extract text (' + _ocrStaged.length + ')';
}

function stageOcrImages(files) {
  const list = files ? Array.prototype.slice.call(files) : [];
  if (!list.length) return;
  _ocrStaged = _ocrStaged.concat(list);
  renderOcrTray();
}

// Run OCR over EVERY staged image in one call so photo-ocr deduplicates the
// overlap. Headers mirror sendImage (bearer + passkey terminal token).
async function runOcrExtraction() {
  const list = _ocrStaged.slice();
  if (!list.length) return;
  const fd = new FormData();
  list.forEach(function (f, i) {
    fd.append('files', f, f.name || ('screenshot-' + (i + 1) + '.png'));
  });
  const btn = els.terminalOcrExtract;
  btn.disabled = true;
  els.terminalScreenshot.disabled = true;
  const stopTimer = startWorkTimer(btn, '📷 Extract text', '⏳ Reading ');
  try {
    const headers = new Headers();
    const bt = readToken();
    if (bt) headers.set('Authorization', 'Bearer ' + bt);
    const tt = readTerminalToken();
    if (tt) headers.set('X-Terminal-Token', tt);
    const res = await fetch('/api/ocr', {
      method: 'POST', headers: headers, body: fd,
    });
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    const body = await res.json().catch(function () { return null; });
    const text = body && body.text;
    const plural = list.length > 1;
    if (!text) {
      toast('📷 No text found in the image' + (plural ? 's' : ''));
      return;
    }
    // Insert at the caret with a leading space when the textarea already
    // has trailing content, so the OCR appends cleanly to typed text.
    const ta = els.terminalComposeInput;
    const before = ta.value.slice(0, ta.selectionStart);
    const sep = (before && !/\s$/.test(before)) ? ' ' : '';
    ta.setRangeText(sep + text, ta.selectionStart, ta.selectionEnd, 'end');
    growComposeInput();
    ta.focus();
    clearOcrStaging();
    toast(
      '📷 Text extracted from ' + list.length + ' image' +
        (plural ? 's' : '') + ' — review, then ➤ Send.',
      'good'
    );
  } catch (exc) {
    toast('OCR failed: ' + (exc.message || exc), 'error');
  } finally {
    stopTimer();
    btn.disabled = false;
    els.terminalScreenshot.disabled = false;
  }
}

// Stop read-aloud whichever voice is active — the hub <audio> and the Web
// Speech queue are independent engines sharing one speaking-state flag, so a
// stop (re-press, tab-leave, new dictation) must silence both.
function stopReading() {
  cancelHub();
  cancelSpeech();
}

// Reflect speaking state on the 🔊 button: ⏹ + pulse while reading, 🔊 idle.
function setSpeakingUI(on) {
  const btn = els.terminalSpeak;
  if (!btn) return;
  btn.classList.toggle('speaking', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.textContent = on ? '⏹' : '🔊';
  btn.title = on ? 'Stop reading' : 'Read the last reply aloud';
}

function wireCompose() {
  els.terminalCompose.addEventListener('click', function () {
    const t = state.terminal;
    if (!t) return;
    setComposeOpen(!t.composeOpen);
  });
  els.terminalRecord.addEventListener('click', function () {
    if (_recorder && _recorder.state === 'recording') stopRecording();
    else startRecording();
  });
  els.terminalScreenshot.addEventListener('click', function () {
    els.terminalScreenshotInput.click();
  });
  els.terminalScreenshotInput.addEventListener('change', function () {
    const picked = els.terminalScreenshotInput.files;
    const list = picked && picked.length
      ? Array.prototype.slice.call(picked) : [];
    els.terminalScreenshotInput.value = '';
    // Stage, don't send — accumulate across taps; Extract collates them all.
    if (list.length) stageOcrImages(list);
  });
  els.terminalOcrExtract.addEventListener('click', runOcrExtraction);
  els.terminalComposeSend.addEventListener('click', function () {
    const t = state.terminal;
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    const text = els.terminalComposeInput.value;
    if (!text) return;
    sendSubmit(t, text);
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
  // 🔊 read the last AI reply aloud (issue #190) — a top-bar control beside
  // ↓ Jump, not in the compose bar (which is for editing). Press while idle →
  // extract the last reply + speak; press while reading → stop. The button
  // tap is the user gesture iOS speech synthesis requires.
  onSpeakingChange(setSpeakingUI);
  // When the reply finishes reading on its own, reset the button (done by
  // setSpeakingUI(false) via onSpeakingChange) and confirm with a toast.
  onSpeechEnd(function () { toast('🔊 Finished reading.', 'good'); });
  els.terminalSpeak.addEventListener('click', async function () {
    if (isSpeaking()) { stopReading(); return; }
    const t = state.terminal;
    if (!t || !t.term) return;
    const text = extractLastReply(t.term);
    if (!text) { toast('🔊 No reply to read yet.'); return; }
    // Visible confirmation the press registered and text was found — so a
    // silent phone (mute switch, volume, BT routing) is distinguishable from
    // an extraction miss. Show the opening words.
    const peek = text.length > 60 ? text.slice(0, 60) + '…' : text;
    // Prefer the hub's high-quality Orpheus voice; fall back to the on-device
    // Web Speech voice on any hub failure (down, blocked, autoplay refused).
    // speakHub() must be called directly here so its synchronous prologue
    // unlocks the <audio> element inside this click's user-gesture tick.
    if (isHubAvailable()) {
      toast('🔊 Reading: ' + peek, 'good');
      try {
        await speakHub(text, {
          token: readToken(),
          terminalToken: readTerminalToken(),
        });
        return;
      } catch (_) {
        // hub path failed — fall through to Web Speech below
      }
    }
    if (!speak(text)) {
      toast('Speech not supported on this browser', 'error');
      return;
    }
    toast('🔊 Reading: ' + peek, 'good');
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
