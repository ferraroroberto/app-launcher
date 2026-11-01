/* A Done row's actions: Issue and PR open GitHub; Proof opens the merged UI
 * PR's stored walkthrough, or a "no proof" chip says the PR declared a UI diff
 * shipped without one. The server decides which (card.proof: 'available' |
 * 'missing', card.proof_pr, card.pr_url); a row with neither shows no Proof.
 * The report is fetched on tap, behind the passkey gate, and shown in the
 * sandboxed HTML viewer (no script, no network): it holds real-app screenshots
 * and never touches GitHub.
 */

import { api, apiFailToast, authHeaders } from '../api.js';
import { ensureTerminalToken } from '../webauthn.js';
import { openTextPreview } from '../lightbox.js';
import { chip } from '../glance.js';

// The row's meta-line chips: "no proof" when a UI PR shipped without a report,
// "Not planned" for an issue closed as not planned; none for any other state.
export function proofGapChips(card) {
  const chips = [];
  if (card.not_planned) chips.push(chip('Not planned', 'neutral', 'board-done-not-planned'));
  if (card.proof === 'missing') chips.push(chip('no proof', 'attention', 'board-proof-none'));
  return chips;
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

function actionButton(label, aria, tone, extraClass, onClick) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'board-done-action';
  btn.setAttribute('aria-label', aria);
  btn.append(chip(label, tone, extraClass));
  btn.addEventListener('click', function () { onClick(btn); });
  return btn;
}

function openGithub(url) {
  return function () { window.open(url, '_blank', 'noopener'); };
}

// The row's trailing actions, each a chip inside a 44px tap target: Issue, PR
// (once a merged PR is known to have closed it) and Proof (once a report is
// stored). Null when the row has nothing to offer.
export function doneActions(card) {
  const buttons = [];
  if (card.url) {
    buttons.push(actionButton('Issue', 'Open issue #' + card.number + ' on GitHub',
      'neutral', 'board-done-issue', openGithub(card.url)));
  }
  if (card.pr_url) {
    buttons.push(actionButton('PR', 'Open PR #' + card.pr + ' on GitHub',
      'neutral', 'board-done-pr', openGithub(card.pr_url)));
  }
  if (card.proof === 'available') {
    buttons.push(actionButton('Proof', 'Open the proof report for ' + card.repo + ' PR #' + card.proof_pr,
      'accent', 'board-proof-chip', function (btn) { openProof(card, btn); }));
  }
  if (!buttons.length) return null;
  const group = document.createElement('div');
  group.className = 'board-done-actions';
  group.append.apply(group, buttons);
  return group;
}
