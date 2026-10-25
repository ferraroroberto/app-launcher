/**
 * "Clear merged (N)" on the Board's Chief's plan card.
 *
 * Finished queue rows stay listed as `merged` until someone removes them, so
 * the card grows over a long run. The tap asks the webapp to prune them
 * (POST /api/board/chief-plan/prune: fleet-config's writer does the edit,
 * the app never touches the plan file) and then re-reads the plan so the card
 * shows what is on disk. The control exists only while N > 0.
 */

import { state } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { terminalJsonApi } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';
import { renderBoard } from './board.js';

const MERGED = 'merged';

// Every merged row in the plan, whatever the Board's repo filter shows: the
// writer clears them all, so the number on the button is the number that goes.
export function mergedCount(plan) {
  return ((plan && plan.queue) || []).filter(function (q) { return q.status === MERGED; }).length;
}

let clearing = false;

async function clearMerged() {
  if (clearing) return;
  clearing = true;
  try {
    const res = await terminalJsonApi('/api/board/chief-plan/prune', { method: 'POST' });
    toast('Cleared ' + res.pruned + (res.pruned === 1 ? ' merged item' : ' merged items'), '',
      { icon: 'circle-check' });
  } catch (exc) {
    apiFailToast('Could not clear merged items', exc);
    return;
  } finally {
    clearing = false;
  }
  // The clear landed; a failed re-read only means the next 5 s poll shows it.
  try {
    state.chiefPlan = await jsonApi('/api/board/chief-plan');
    renderBoard();
  } catch (_exc) { /* the poll picks it up */ }
}

export function clearMergedButton(n) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'button-tint board-plan-clear-merged';
  btn.innerHTML = icon('trash-2') + ' ';
  btn.appendChild(document.createTextNode('Clear merged (' + n + ')'));
  btn.addEventListener('click', clearMerged);
  return btn;
}
