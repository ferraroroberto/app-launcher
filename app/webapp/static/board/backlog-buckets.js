/* The Backlog lane, grouped by where each open issue stands.
 *
 * The server stamps every open issue with exactly one `bucket` and its reason
 * chips (src/board/backlog_buckets.py); names and counting are
 * plan-buckets.js's, shared with the Chief's plan card. In progress, Waiting
 * on you, Queued, Blocked and Not planned draw open; Umbrellas, Records and
 * Dormant draw folded shut but are counted like the rest, and a sum line at
 * the top adds every bucket up to the lane's total, so nothing is hidden.
 * `unknown` (a source that could place it could not be read) leads, and is
 * never shown as Not planned. A payload with no buckets (an older server, a
 * stubbed test body) keeps the flat list.
 */

import { chip } from '../glance.js';
import { foldGroup } from './chief-plan.js';
import { BUCKETS, bucketCounts } from './plan-buckets.js';

// The buckets drawn open, top to bottom, with their chip tone.
const OPEN = [
  { key: 'unknown', cls: 'is-unknown', hideEmpty: true },
  { key: 'in_progress', tone: 'accent' },
  { key: 'waiting', tone: 'attention', cls: 'is-waiting' },
  { key: 'queued', tone: 'neutral' },
  { key: 'blocked', tone: 'neutral', cls: 'is-blocked' },
  { key: 'not_planned', cls: 'is-not-planned' },
];

const HEADINGS = {};
BUCKETS.forEach(function (b) { HEADINGS[b.key] = b.heading; });

const TONES = {};
OPEN.forEach(function (b) { TONES[b.key] = b.tone || 'neutral'; });

export function hasBuckets(cards) {
  return cards.some(function (card) { return typeof card.bucket === 'string'; });
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
  h.textContent = HEADINGS[spec.key] + ' (' + rows.length + ')' + (note ? ' · ' + note : '');
  const list = document.createElement('ul');
  list.className = 'card-list board-list';
  rows.forEach(function (row) { list.appendChild(row); });
  li.append(h, list);
  return li;
}

// One folded bucket, shut until opened; nothing when it is empty.
// `opts.track` is the fold's usage-log id (a builder in .fleet.toml).
function foldedBucket(opts) {
  if (!opts.rows.length) return null;
  const li = document.createElement('li');
  li.className = 'board-bucket is-folded';
  li.dataset.bucket = opts.key;
  li.appendChild(foldGroup({
    key: 'backlog:' + opts.key,
    track: opts.track,
    heading: HEADINGS[opts.key] + ' (' + opts.rows.length + ')',
    rows: opts.rows,
    defaultOpen: false,
    cls: 'board-bucket-fold',
  }));
  return li;
}

// "105 open issues: 2 in progress · 7 waiting on you · …": every non-empty
// bucket, so the parts visibly add up to the lane's count.
export function sumLine(cards) {
  const tally = bucketCounts(cards);
  const parts = [];
  BUCKETS.forEach(function (b) {
    const n = tally.counts[b.key];
    if (n) parts.push(n + ' ' + b.heading.toLowerCase());
  });
  const head = tally.total + (tally.total === 1 ? ' open issue' : ' open issues');
  return parts.length ? head + ': ' + parts.join(' · ') : head;
}

// What the total cannot promise: a fetch that hit GitHub's ceiling, and a
// fleet scope that could not be read (dormant repos then sit in the open
// buckets).
function sumNotes(body) {
  const notes = [];
  if (body && body.github && body.github.truncated) notes.push('GitHub returned its first 1000 open issues; more exist');
  const health = (body && body.backlog_buckets) || {};
  if (health.scope === 'unknown') notes.push('fleet scope unknown, so dormant repos are not set aside');
  return notes;
}

function sumItem(cards, body) {
  const li = document.createElement('li');
  li.className = 'board-bucket board-bucket-sum';
  const p = document.createElement('p');
  p.className = 'board-plan-line muted small board-bucket-sum-line';
  p.textContent = [sumLine(cards)].concat(sumNotes(body)).join(' · ');
  li.appendChild(p);
  return li;
}

// The lane's children: the sum line, one group per open bucket, then the
// folded buckets. `renderRow` draws one issue card (board.js's renderIssueCard).
export function bucketNodes(cards, body, renderRow) {
  const byBucket = {};
  cards.forEach(function (card) {
    (byBucket[card.bucket] = byBucket[card.bucket] || []).push(card);
  });
  const health = (body && body.backlog_buckets) || {};
  const nodes = [sumItem(cards, body)];
  OPEN.forEach(function (spec) {
    const rows = (byBucket[spec.key] || []).map(renderRow);
    if (spec.hideEmpty && !rows.length) return;
    nodes.push(bucketGroup(spec, rows, spec.key === 'unknown' ? health.unknown_reason : ''));
  });
  const rowsOf = function (key) { return (byBucket[key] || []).map(renderRow); };
  [
    foldedBucket({ key: 'umbrella', track: 'board.backlog-umbrella-fold', rows: rowsOf('umbrella') }),
    foldedBucket({ key: 'record', track: 'board.backlog-record-fold', rows: rowsOf('record') }),
    foldedBucket({ key: 'dormant', track: 'board.backlog-dormant-fold', rows: rowsOf('dormant') }),
  ].forEach(function (node) { if (node) nodes.push(node); });
  return nodes;
}
