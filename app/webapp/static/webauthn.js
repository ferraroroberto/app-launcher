/* Passkey enrollment + per-session unlock for the live terminal.
 *
 * The terminal token (returned by `auth/finish`) lives in localStorage
 * for its TTL — re-opening a session within the window skips the
 * passkey UI. Loopback callers bypass the gate entirely.
 */

import { els, state, TT_KEY, TT_EXP_KEY } from './state.js';
import { jsonApi, toast } from './api.js';

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

function prepCreate(o) {
  o.challenge = b64urlToBuf(o.challenge);
  o.user.id = b64urlToBuf(o.user.id);
  (o.excludeCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
  return o;
}

function prepGet(o) {
  o.challenge = b64urlToBuf(o.challenge);
  (o.allowCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
  return o;
}

function serializeReg(c) {
  return {
    id: c.id,
    rawId: bufToB64url(c.rawId),
    type: c.type,
    response: {
      attestationObject: bufToB64url(c.response.attestationObject),
      clientDataJSON: bufToB64url(c.response.clientDataJSON),
    },
    clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
    authenticatorAttachment: c.authenticatorAttachment || undefined,
  };
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
export async function fetchWebauthnStatus() {
  try {
    state.webauthn = await jsonApi('/api/webauthn/status');
    renderWebauthn();
  } catch (_) { /* best-effort */ }
}

function renderWebauthn() {
  const w = state.webauthn || {};
  if (!els.webauthnStatus) return;
  if (!w.configured) {
    els.webauthnStatus.textContent =
      'Passkey gate not configured — the terminal is Tailscale-only.';
    els.webauthnDevices.innerHTML = '';
    els.enrollDeviceBtn.hidden = true;
    return;
  }
  const n = (w.devices || []).length;
  let msg = n ? n + ' device(s) enrolled.' : 'No device enrolled yet.';
  if (w.enrollment_open) {
    msg += ' Enrollment window open (' + w.enrollment_seconds_left + 's).';
  }
  els.webauthnStatus.textContent = msg;
  els.webauthnDevices.innerHTML = '';
  (w.devices || []).forEach(function (d) {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = d.label + ' · ' +
      (d.last_used ? 'last used ' + d.last_used : 'added ' + d.added_at);
    li.appendChild(label);
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'icon-btn danger';
    rm.textContent = '🗑️';
    rm.title = 'Remove passkey';
    rm.addEventListener('click', function () { removeDevice(d); });
    li.appendChild(rm);
    els.webauthnDevices.appendChild(li);
  });
  els.enrollDeviceBtn.hidden = !w.enrollment_open;
}

async function removeDevice(d) {
  if (!confirm('Remove passkey "' + d.label + '"?')) return;
  try {
    await jsonApi('/api/webauthn/devices/' + encodeURIComponent(d.id), {
      method: 'DELETE',
    });
    toast('Removed ' + d.label, 'good');
    fetchWebauthnStatus();
  } catch (exc) {
    toast('Remove failed: ' + (exc.message || exc), 'error');
  }
}

async function enrollDevice() {
  if (!window.PublicKeyCredential) {
    toast('This browser has no passkey support.', 'error');
    return;
  }
  const label = prompt('Name this device', 'iPhone');
  if (!label) return;
  try {
    const opts = await jsonApi('/api/webauthn/enroll/begin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: label }),
    });
    const cred = await navigator.credentials.create({
      publicKey: prepCreate(opts),
    });
    await jsonApi('/api/webauthn/enroll/finish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serializeReg(cred)),
    });
    toast('✅ Device enrolled.', 'good');
    fetchWebauthnStatus();
  } catch (exc) {
    toast('Enrollment failed: ' + (exc.message || exc), 'error');
  }
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

export function wireWebauthn() {
  els.enrollDeviceBtn.addEventListener('click', enrollDevice);
}
