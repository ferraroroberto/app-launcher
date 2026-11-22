/* The build readout's restart sheet, wired to this app's auth.
 *
 * The sheet itself is the vendored page-foot restart sheet; this module hands
 * it the two requests it makes. Reads go through jsonApi (the bearer token).
 * The POST also carries the passkey terminal token, because the server gates
 * a restart like the routes that write into a live PTY.
 *
 * The session-host target's warning names how many live sessions the restart
 * ends. The vendored sheet re-reads the target list each time it opens, so the
 * count it shows is the one read then, not the one from page load.
 */

import { initRestartSheet } from '../_vendored/page-foot/restart-sheet.js';
import { AuthRequiredError, apiRaw, jsonApi } from '../api.js';
import { ensureTerminalToken } from '../webauthn.js';

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

export async function wireBuildRestart(readout) {
  await initRestartSheet({
    readout,
    fetchJson: jsonApi,
    postJson: postRestart,
  });
}
