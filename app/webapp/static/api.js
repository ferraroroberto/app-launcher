/* HTTP helpers + bearer-token plumbing + login overlay + toast.
 *
 * Every module reaches the server through api()/jsonApi() so the
 * Authorization: Bearer header is attached once, and 401s flip the
 * login overlay open from one place.
 */

import { els, state, TOKEN_KEY } from './state.js';

// --------------------------------------------------------------- tokens
// Read a `?<name>=<value>` deep-link param once, strip it from the visible
// URL, and return its trimmed value (or null if absent/blank). Used for the
// bearer ?token=, the PC-mirror ?terminal=<sid> deep link, and the Board
// ?board=<sid> deep link (issue #301).
export function consumeUrlParam(name) {
  const params = new URLSearchParams(window.location.search);
  const v = (params.get(name) || '').trim();
  if (!v) return null;
  params.delete(name);
  const q = params.toString();
  window.history.replaceState(
    {}, '',
    window.location.pathname + (q ? '?' + q : '') + window.location.hash
  );
  return v;
}

export function readToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
export function writeToken(t) { if (t) localStorage.setItem(TOKEN_KEY, t); }

// Build request headers: bearer token (always, when present) plus the
// passkey terminal token, when the caller passes one — callers own how they
// obtained it (readTerminalToken() for the cached value, or an awaited
// ensureTerminalToken() when a fresh passkey ceremony may be needed), since
// that plumbing lives in webauthn.js and this module never imports it.
// `contentType`, when given, sets Content-Type (omit for multipart/FormData
// bodies, where the browser must set its own boundary). Single source for
// what used to be three differently-cased terminal-token header helpers
// (X-Terminal-Token / x-terminal-token / terminalHeaders) — always
// `X-Terminal-Token` now.
export function authHeaders({ terminalToken, contentType } = {}) {
  const h = {};
  const bearer = readToken();
  if (bearer) h['Authorization'] = 'Bearer ' + bearer;
  if (terminalToken) h['X-Terminal-Token'] = terminalToken;
  if (contentType) h['Content-Type'] = contentType;
  return h;
}

// True when this browser is a desktop (a fine/mouse pointer), as opposed
// to a phone (a coarse/touch pointer). A coding launch carries this so the
// server can skip the PC mirror window (issue #159): a desktop browser
// already renders the streamed terminal in-page, so spawning a separate
// Edge --app window is redundant — the redundancy only surfaced when the
// desktop reached the app over the tunnel (non-loopback), where the server
// can't tell it's the PC by IP alone. A phone keeps the mirror.
export function isDesktopClient() {
  try {
    return window.matchMedia('(pointer: fine)').matches;
  } catch (exc) {
    return false;
  }
}

// A 401 means the login overlay just went up — callers that merely want to
// swallow "not logged in yet" (rather than toast it as a real failure) check
// `exc instanceof AuthRequiredError`, not a string message that a future
// reword would silently break.
export class AuthRequiredError extends Error {
  constructor() {
    super('auth required');
    this.name = 'AuthRequiredError';
  }
}

// --------------------------------------------------------------- fetch
export async function api(path, opts) {
  opts = opts || {};
  const headers = new Headers(opts.headers || {});
  const token = readToken();
  if (token) headers.set('Authorization', 'Bearer ' + token);
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    showLogin();
    throw new AuthRequiredError();
  }
  return res;
}

export async function jsonApi(path, opts) {
  const res = await api(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) {
    const detail = (body && body.detail) || ('HTTP ' + res.status);
    const err = new Error(detail);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

// --------------------------------------------------------------- login
export function showLogin() {
  if (!els.loginOverlay) return;
  els.loginOverlay.hidden = false;
  els.loginPassword.value = '';
  els.loginPassword.focus();
}

export function hideLogin() {
  if (els.loginOverlay) els.loginOverlay.hidden = true;
}

// Boot hook called from main.js — passed `onLoginSuccess` so this module
// stays independent of the boot sequence.
export function wireLoginForm(onLoginSuccess) {
  els.loginForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    els.loginError.hidden = true;
    const password = els.loginPassword.value;
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      const body = await res.json().catch(function () { return null; });
      if (!res.ok || !body || !body.token) {
        const msg = (body && body.detail) || 'Login failed';
        els.loginError.textContent = msg;
        els.loginError.hidden = false;
        return;
      }
      writeToken(body.token);
      hideLogin();
      onLoginSuccess();
    } catch (exc) {
      els.loginError.textContent = String(exc.message || exc);
      els.loginError.hidden = false;
    }
  });
}

// --------------------------------------------------------------- toast
let toastTimer = null;
export function toast(msg, kind) {
  els.toast.textContent = msg;
  els.toast.className = 'toast ' + (kind || '');
  els.toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    els.toast.hidden = true;
  }, kind === 'error' ? 4500 : 2200);
}

// Toast a failed API call as "<prefix>: <message>" (error styling) — unless
// exc is an AuthRequiredError, in which case the login overlay already went
// up and a red toast on top of it would double-fire. `prefix` may be falsy
// for the few sites that just toast the raw message.
export function apiFailToast(prefix, exc) {
  if (exc instanceof AuthRequiredError) return;
  const msg = (exc && exc.message) || exc;
  toast(prefix ? prefix + ': ' + msg : String(msg), 'error');
}
