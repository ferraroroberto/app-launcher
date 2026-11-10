/* A red GitHub CI row in PRs and jobs: one per failing workflow on a fleet
 * repo's default branch, and one "unknown" row per repo whose runs could not
 * be read (never drawn as green). The server builds the cards from its CI
 * sweep (src/board/ci_runs.py); a later green run drops the row.
 */

import { actionRow } from '../action-rows.js';
import { chip } from '../glance.js';
import { fmtDuration } from '../shared/format.js';

// A failed workflow is danger, like a failed Job; an unread repo wants a look.
const CI_CHIPS = {
  failed: ['CI failed', 'danger'],
  unknown: ['CI unknown', 'attention'],
};

export function ciFailing(card) {
  return card.kind === 'ci' && card.state === 'failed';
}

export function renderCiCard(card) {
  const c = CI_CHIPS[card.state] || CI_CHIPS.unknown;
  const failed = card.state === 'failed';
  const meta = [
    card.workflow || '',
    failed && card.age_seconds != null ? 'red ' + fmtDuration(card.age_seconds) : '',
    failed ? '' : 'runs not read',
  ].filter(Boolean).join(' · ');
  const url = failed ? card.run_url : null;
  const row = actionRow({
    className: 'board-item board-item-ci is-' + card.state,
    title: card.repo || 'repo',
    meta: meta,
    chips: [chip(c[0], c[1], 'board-ci-chip')],
    label: url ? 'Open the failed ' + (card.workflow || 'CI') + ' run of ' + card.repo + ' on GitHub' : '',
    onMain: url ? function () { window.open(url, '_blank', 'noopener'); } : null,
    inert: !url,
  });
  if (url) row.main.dataset.track = 'board.ci-run';
  if (card.error) row.main.title = card.error;
  return row.li;
}
