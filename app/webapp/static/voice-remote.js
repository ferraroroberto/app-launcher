/* The Remote Desktop hint for a missing microphone.
 *
 * A Windows Remote Desktop session has no mic of its own: dictation in the
 * PC's own browser only works when the RDP client redirects its microphone.
 * The server reads the PC's session kind live (GET /api/desktop-session:
 * `remote` / `local` / `unknown`, plus whether the caller is a browser on
 * this PC). Only `remote` from this PC's own browser earns the specific
 * message; `local`, `unknown`, a phone, another computer or a failed read all
 * keep the generic one, so a wrong guess is never shown.
 */

import { jsonApi, logPollFailure } from './api.js';
import { els } from './state.js';

export const REMOTE_MIC_HINT =
  "No microphone in this remote-desktop session. Turn on 'Record from this computer' " +
  "in your Remote Desktop client, or open the launcher in this computer's own browser over Tailscale.";

// True only when the PC's session is a Remote Desktop one and the caller is
// a browser on this PC; false on every other answer and on a failed read.
export async function inRemoteDesktopSession() {
  try {
    const body = await jsonApi('/api/desktop-session');
    return !!body && body.state === 'remote' && body.this_pc === true;
  } catch (exc) {
    logPollFailure('desktop session', exc);
    return false;
  }
}

// Settings' Voice line: shown while the session is remote, hidden otherwise.
export async function refreshRemoteVoiceNote() {
  if (!els.voiceRemoteHelp) return;
  const remote = await inRemoteDesktopSession();
  els.voiceRemoteHelp.textContent = remote ? REMOTE_MIC_HINT : '';
  document.querySelectorAll('[data-voice-remote]').forEach(function (node) {
    node.hidden = !remote;
  });
}
