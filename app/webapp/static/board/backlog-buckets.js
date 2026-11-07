/* The Backlog lane, grouped by where each open issue stands.
 *
 * The server stamps every backlog card with a `bucket` and its reason chips
 * (src/board/backlog_buckets.py): In progress, Waiting on you, Queued, Not
 * planned, plus `folded` (umbrellas and machine records, one collapsed row
 * outside every count) and `unknown` (a source that could place it could
 * not be read, so it is never shown as Not planned). A payload with no
 * buckets (an older server, a stubbed test body) keeps the flat list.
 */

import { chip } from '../glance.js';
import { foldGroup } from './chief-plan.js';

// Drawn top to bottom. Unknown leads: it is a warning, not a plan state.
const BUCKETS = [
  { key: 'unknown', heading: 'Unknown', cls: 'is-unknown', hideEmpty: true },
  { key: 'in_progress', heading: 'In progress', tone: 'accent' },
  { key: 'waiting', heading: 'Waiting on you', tone: 'attention', cls: 'is-waiting' },
  { key: 'queued', heading: 'Queued', tone: 'neutral' },
  { key: 'not_planned', heading: 'Not planned', cls: 'is-not-planned' },
];

const TONES = {};
BUCKETS.forEach(function (b) { TONES[b.key] = b.tone || 'neutral'; });

export function hasBuckets(cards) {
  return cards.some(function (card) { return typeof card.bucket === 'string'; });
}

// The cards the lane counts: folded rows stay out, as the server documents.
export function countedCards(cards) {
  return cards.filter(function (card) { return card.bucket !== 'folded'; });
}

export function notPlannedCount(cards) {
  return cards.filter(function (card) { return card.bucket === 'not_planned'; }).length;
}

// A card's bucket reasons as one chip in that bucket's tone: chips never
// shrink, so a second one would push the repo and number off a phone row.
export function bucketChipEls(card) {
  const reasons = (card.bucket_chips || []).map(String).filter(Boolean);
  if (!reasons.length) return [];
  return [chip(reasons.join(' · '), TONES[card.bucket] || 'neutral', 'board-bucket-chip')];
}

function bucketGroup(spec, rows, note) {
  const li = document.createElement('li');
  li.className = 'board-bucket ' + (spec.cls || '');
  li.dataset.bucket = spec.key;
  const h = document.createElement('h4');
  h.className = 'board-plan-heading board-bucket-heading';
  h.textContent = spec.heading + ' (' + rows.length + ')' + (note ? ' · ' + note : '');
  const list = document.createElement('ul');
  list.className = 'card-list board-list';
  rows.forEach(function (row) { list.appendChild(row); });
  li.append(h, list);
  return li;
}

// The lane's children: one group per bucket, then the folded row.
// `renderRow` draws one issue card (board.js's renderIssueCard).
export function bucketNodes(cards, body, renderRow) {
  const byBucket = {};
  cards.forEach(function (card) {
    (byBucket[card.bucket] = byBucket[card.bucket] || []).push(card);
  });
  const health = (body && body.backlog_buckets) || {};
  const nodes = [];
  BUCKETS.forEach(function (spec) {
    const rows = (byBucket[spec.key] || []).map(renderRow);
    if (spec.hideEmpty && !rows.length) return;
    nodes.push(bucketGroup(spec, rows, spec.key === 'unknown' ? health.unknown_reason : ''));
  });
  const folded = byBucket.folded || [];
  if (folded.length) {
    const li = document.createElement('li');
    li.className = 'board-bucket is-folded';
    li.dataset.bucket = 'folded';
    li.appendChild(foldGroup({
      key: 'backlog:folded',
      track: 'board.backlog-folded',
      heading: 'Umbrellas and records (' + folded.length + ')',
      rows: folded.map(renderRow),
      defaultOpen: false,
      cls: 'board-bucket-fold',
    }));
    nodes.push(li);
  }
  return nodes;
}
