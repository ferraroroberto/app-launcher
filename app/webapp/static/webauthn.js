/* Passkey enrollment + per-session unlock for the live terminal.
 *
 * The terminal token (returned by `auth/finish`) lives in localStorage
 * for its TTL — re-opening a session within the window skips the
 * passkey UI. Loopback callers bypass the gate entirely.
 */

import { state, TT_KEY, TT_EXP_KEY } from './state.js';
import { jsonApi } from './api.js';

// ----------------------------------------------------------- b64url helpers
function b64urlToBuf(s) {
  s = String(s).replace(/-/g, '+').replace(/_/g, '/');
  const pad = s.length % 4 ? '='.repeat(4 - (s.length % 4)) : '';
  const bin = atob(s + pad);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

function bufToB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function prepGet(o) {
  o.challenge = b64urlToBuf(o.challenge);
  (o.allowCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
  return o;
}

function serializeAuth(c) {
  return {
    id: c.id,
    rawId: bufToB64url(c.rawId),
    type: c.type,
    response: {
      authenticatorData: bufToB64url(c.response.authenticatorData),
      clientDataJSON: bufToB64url(c.response.clientDataJSON),
      signature: bufToB64url(c.response.signature),
      userHandle: c.response.userHandle ? bufToB64url(c.response.userHandle) : null,
    },
    clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
    authenticatorAttachment: c.authenticatorAttachment || undefined,
  };
}

// ----------------------------------------------------------- terminal token store
export function readTerminalToken() {
  const tok = localStorage.getItem(TT_KEY);
  const exp = parseInt(localStorage.getItem(TT_EXP_KEY) || '0', 10);
  if (tok && exp > Date.now()) return tok;
  return '';
}

export function writeTerminalToken(tok, ttlSeconds) {
  if (!tok) return;
  localStorage.setItem(TT_KEY, tok);
  localStorage.setItem(
    TT_EXP_KEY, String(Date.now() + (ttlSeconds || 3600) * 1000)
  );
}

export function clearTerminalToken() {
  localStorage.removeItem(TT_KEY);
  localStorage.removeItem(TT_EXP_KEY);
}

// ----------------------------------------------------------- webauthn flows
// The Settings passkey section (status readout, device list, enroll
// button) was removed in the #383 review round — the gate itself stays:
// fetchWebauthnStatus still populates state.webauthn, which
// ensureTerminalToken keys on. Enrollment now happens from the PC via the
// tray's enrollment window (the /api/webauthn/enroll/* endpoints are
// untouched); wire a UI back here if phone-side enrollment returns.
export async function fetchWebauthnStatus() {
  try {
    state.webauthn = await jsonApi('/api/webauthn/status');
  } catch (_) { /* best-effort */ }
}

async function unlockTerminal() {
  if (!window.PublicKeyCredential) {
    throw new Error('this browser has no passkey support');
  }
  const opts = await jsonApi('/api/webauthn/auth/begin', { method: 'POST' });
  const cred = await navigator.credentials.get({ publicKey: prepGet(opts) });
  const body = await jsonApi('/api/webauthn/auth/finish', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(serializeAuth(cred)),
  });
  writeTerminalToken(body.terminal_token, body.ttl_seconds);
  return body.terminal_token;
}

export async function ensureTerminalToken() {
  if (!state.webauthn || !state.webauthn.configured) return '';
  // On the PC itself (loopback) the server bypasses the passkey gate —
  // and the iPhone's passkey isn't on this device anyway. Skip it.
  if (state.status && state.status.terminal &&
      state.status.terminal.reason === 'loopback') {
    return '';
  }
  const existing = readTerminalToken();
  if (existing) return existing;
  return await unlockTerminal();
}
