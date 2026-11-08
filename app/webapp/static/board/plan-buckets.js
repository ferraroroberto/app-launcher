/* The Board's one per-issue partition, read on the client.
 *
 * The server stamps every open issue with exactly one `bucket`
 * (src/board/backlog_buckets.py). The Backlog lane, the Chief's plan card and
 * Your turn's owner rows all read that stamp through this module, so a bucket
 * has one name and one count everywhere. Nothing here re-derives a bucket
 * from the plan or the labels.
 *
 * Pure, with no imports, so plain Node can pin it (tests/js/plan_buckets.test.mjs).
 */

// Every bucket's name, in the order the Backlog draws them. `folded` buckets
// draw shut; every bucket is counted in the total.
export const BUCKETS = [
  { key: 'unknown', heading: 'Unknown' },
  { key: 'in_progress', heading: 'In progress' },
  { key: 'waiting', heading: 'Waiting on you' },
  { key: 'queued', heading: 'Queued' },
  { key: 'blocked', heading: 'Blocked' },
  { key: 'not_planned', heading: 'Not planned' },
  { key: 'umbrella', heading: 'Umbrellas', folded: true },
  { key: 'record', heading: 'Records', folded: true },
  { key: 'dormant', heading: 'Dormant', folded: true },
];

// A plan row whose issue is not open on the Backlog (merged, closed), and one
// that names no issue at all.
export const CLOSED = 'closed';
export const NOT_AN_ISSUE = 'not_an_issue';

const NAMES = {};
BUCKETS.forEach(function (b) { NAMES[b.key] = b.heading; });
NAMES[CLOSED] = 'Closed';
NAMES[NOT_AN_ISSUE] = 'Not an issue';
// The plan card's catch-all for a row no named group takes.
NAMES.other = 'Other';

export function bucketName(key) {
  return NAMES[key] || String(key || 'unknown');
}

const REF = /^([A-Za-z0-9._-]*)#(\d+)$/;

// `repo#N`, lower-cased, from a card, or from a plan row's `ref` (a bare `#N`
// takes the row's `repo`); '' when it names no issue.
export function issueKey(repo, ref) {
  const m = REF.exec(String(ref || '').trim());
  if (!m) return '';
  const name = (m[1] || String(repo || '')).trim().toLowerCase();
  return name ? name + '#' + m[2] : '';
}

function cardKey(card) {
  return card.repo && Number.isInteger(card.number) ? issueKey(card.repo, '#' + card.number) : '';
}

// Each open issue's bucket by `repo#N`.
export function bucketIndex(cards) {
  const index = {};
  (cards || []).forEach(function (card) {
    const key = cardKey(card);
    if (key && typeof card.bucket === 'string') index[key] = card.bucket;
  });
  return index;
}

// How many cards each bucket holds, and their sum.
export function bucketCounts(cards) {
  const counts = {};
  BUCKETS.forEach(function (b) { counts[b.key] = 0; });
  (cards || []).forEach(function (card) {
    if (typeof card.bucket === 'string') counts[card.bucket] = (counts[card.bucket] || 0) + 1;
  });
  let total = 0;
  Object.keys(counts).forEach(function (k) { total += counts[k]; });
  return { counts: counts, total: total };
}

// The plan's queue rows grouped by their issue's bucket, one row per issue
// (the server places an issue by its first row, so a repeat row is dropped):
// `{bucket: [row, ...]}`, with CLOSED for an issue the Backlog does not hold
// and NOT_AN_ISSUE for a row that names none.
export function groupPlanRows(rows, index) {
  const groups = {};
  const seen = {};
  (rows || []).forEach(function (row) {
    const key = issueKey(row.repo, row.ref);
    if (key && seen[key]) return;
    if (key) seen[key] = true;
    const bucket = !key ? NOT_AN_ISSUE : (index[key] || CLOSED);
    (groups[bucket] = groups[bucket] || []).push(row);
  });
  return groups;
}
