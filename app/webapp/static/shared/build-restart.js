/* The build readout's restart sheet, wired to this app's auth.
 *
 * The sheet itself is the vendored page-foot restart sheet; this module hands
 * it the two requests it makes. Reads go through jsonApi (the bearer token).
 * The POST also carries the passkey terminal token, because the server gates
 * a restart like the routes that write into a live PTY.
 *
 * The session-host target's warning names how many live sessions the restart
 * ends. The vendored sheet reads its target list once, at boot, and shows a
 * target's `warning` when it asks the second time, so the list it holds is
 * refreshed in place every time the sheet opens or its target changes: the
 * count it shows is the one read then, not the one from page load.
 */

import { initRestartSheet } from '../_vendored/page-foot/restart-sheet.js';
import { AuthRequiredError, apiRaw, jsonApi } from '../api.js';
import { ensureTerminalToken } from '../webauthn.js';

const RESTART_ENDPOINT = '/api/restart';

// The very target objects the vendored sheet holds; refreshed in place.
let heldTargets = null;

function mergeTargets(held, fresh) {
  if (!held) return fresh;
  for (const target of fresh || []) {
    const mine = held.find((t) => t.id === target.id);
    if (mine) mine.warning = target.warning;
  }
  return held;
}

async function fetchRestartJson(url) {
  const body = await jsonApi(url);
  if (url === RESTART_ENDPOINT && body && Array.isArray(body.targets)) {
    heldTargets = mergeTargets(heldTargets, body.targets);
    body.targets = heldTargets;
  }
  return body;
}

async function postRestart(url, body) {
  let terminalToken;
  try {
    terminalToken = await ensureTerminalToken();
  } catch (_) {
    // A cancelled or failed passkey unlock: the sheet says "not allowed".
    return { status: 401, body: null };
  }
  let res;
  try {
    res = await apiRaw(url, {
      method: 'POST',
      terminalToken,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return { status: 401, body: null };
    throw exc;
  }
  let data = null;
  try { data = await res.json(); } catch (_) { data = null; }
  return { status: res.status, body: data };
}

function refreshTargets() {
  fetchRestartJson(RESTART_ENDPOINT).catch(() => {});
}

export async function wireBuildRestart(readout) {
  const offered = await initRestartSheet({
    readout,
    fetchJson: fetchRestartJson,
    postJson: postRestart,
  });
  if (!offered) return;
  readout.addEventListener('click', refreshTargets);
  readout.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') refreshTargets();
  });
  const sheet = document.querySelector('dialog[data-surface="app.restart-sheet"]');
  if (sheet) sheet.addEventListener('change', refreshTargets);
}
