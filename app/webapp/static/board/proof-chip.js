/* A Done row's visual proof: a "Proof" chip that opens the merged UI PR's
 * stored walkthrough, or a "no proof" chip where the PR said a UI diff shipped
 * without one. The server decides which (card.proof: 'available' | 'missing',
 * card.proof_pr); a row with neither shows nothing. The report is fetched on
 * tap, behind the passkey gate, and shown in the sandboxed HTML viewer (no
 * script, no network): it holds real-app screenshots and never touches GitHub.
 */

import { api, apiFailToast, authHeaders } from '../api.js';
import { ensureTerminalToken } from '../webauthn.js';
import { openTextPreview } from '../lightbox.js';
import { chip } from '../glance.js';

// The "no proof" chip, for the row's meta line; none for any other state.
export function proofGapChips(card) {
  return card.proof === 'missing' ? [chip('no proof', 'attention', 'board-proof-none')] : [];
}

async function openProof(card, btn) {
  btn.disabled = true;
  try {
    const tt = await ensureTerminalToken();
    const res = await api(
      '/api/board/proof/' + encodeURIComponent(card.repo) + '/' + encodeURIComponent(card.proof_pr),
      { headers: authHeaders({ terminalToken: tt }) },
    );
    if (!res.ok) {
      throw new Error(res.status === 404 ? 'no proof report is stored for that PR' : 'status ' + res.status);
    }
    // Named as the store names it; the viewer's Download saves it under that name.
    openTextPreview(card.repo + '-' + card.proof_pr + '.html', await res.text(), 'html', btn);
  } catch (exc) {
    apiFailToast('Could not open the proof report', exc);
  } finally {
    btn.disabled = false;
  }
}

// The row's trailing Proof control: a chip inside a 44px tap target. Null when
// the row has no stored report.
export function proofButton(card) {
  if (card.proof !== 'available') return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'board-proof-open';
  btn.setAttribute('aria-label', 'Open the proof report for ' + card.repo + ' PR #' + card.proof_pr);
  btn.append(chip('Proof', 'accent', 'board-proof-chip'));
  btn.addEventListener('click', function () { openProof(card, btn); });
  return btn;
}
