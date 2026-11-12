/* The Board's Ideas card: what the daily inspiration scan kept, and what
 * became of each idea.
 *
 * GET /api/inspiration/ideas serves the undecided ideas (best score first) and
 * the ledger of decided ones (newest first); the chief is the ledger's only
 * writer, so the card only reads. A segment picks New, Applied or Filtered;
 * each list shows three rows, then "N more" unfolds the rest in place. A tap
 * opens the idea's sheet (idea-sheet.js), where the owner files or drops it.
 *
 * The card is hidden until the scan has written anything ("missing"). A file
 * that cannot be read is its own unknown line, never "0 ideas". The read rides
 * the Board poll but runs at most every IDEAS_POLL_MS: the scan writes once a
 * day and the chief records an outcome only after a send.
 */

import { jsonApi } from '../api.js';
import { actionRow } from '../action-rows.js';
import { chip } from '../glance.js';
import { el } from '../dom-utils.js';
import { chevron, chiefRunState } from './chief-plan.js';
import { ideaDate, openIdeaSheet, scoreText, sourceText, syncIdeaSheet, wireIdeaSheet } from './idea-sheet.js';

const IDEAS_POLL_MS = 30 * 1000;
const SHOWN_ROWS = 3;

let data = null;
let readAt = 0;
let inFlight = false;
let segment = 'new';
// Segments unfolded past their first three rows.
const unfolded = {};
// Ideas sent to the chief from this page, id -> 'file' | 'drop', until the
// ledger records them: the row says so and the sheet cannot send it twice.
const sent = {};
// The run state the Board last read, for a sheet opened between polls.
let chiefRun = '';

function card() { return document.getElementById('boardIdeas'); }

function rowMeta(entry) {
  if (entry.state === 'applied') return ['Filed ' + ideaDate(entry.at), sourceText(entry)].join(' · ');
  if (entry.state === 'filtered') {
    return ['Dropped ' + ideaDate(entry.at), entry.note || sourceText(entry)].join(' · ');
  }
  return [scoreText(entry.score), sourceText(entry), ideaDate(entry.published)].filter(Boolean).join(' · ');
}

function ideaRow(entry) {
  const open = function () { openIdeaSheet(entry, sheetOpts()); };
  const chips = [];
  if (entry.state === 'applied' && entry.issue) chips.push(chip(entry.issue, 'neutral', 'board-idea-issue'));
  if (!entry.state && sent[entry.id]) chips.push(chip('Sent', 'neutral', 'board-idea-sent'));
  const row = actionRow({
    className: 'board-item board-idea',
    id: entry.id,
    title: entry.title || entry.link || 'Untitled idea',
    meta: rowMeta(entry),
    chips: chips,
    label: 'Open the idea: ' + (entry.title || 'untitled'),
    onMain: open,
    trailing: chevron('chevron-right', open),
  });
  row.main.dataset.track = 'board.idea-open';
  return row.li;
}

function sheetOpts() {
  return {
    run: chiefRun,
    sent: sent,
    onSent: function (id, action) {
      sent[id] = action;
      paint();
      readAt = 0;
    },
  };
}

function entriesFor(seg) {
  if (!data || data.state !== 'ok') return [];
  if (seg === 'new') return data.ideas;
  return data.ledger.filter(function (r) { return r.state === seg; });
}

function moreRow(hidden) {
  const li = el('li', 'board-ideas-more-row');
  const b = el('button', 'button-ghost board-ideas-more', hidden + ' more');
  b.type = 'button';
  b.dataset.track = 'board.ideas-more';
  b.addEventListener('click', function () {
    unfolded[segment] = true;
    paint();
  });
  li.appendChild(b);
  return li;
}

function emptyText(seg) {
  if (seg === 'new') return 'No new ideas. The next scan adds more.';
  return seg === 'applied' ? 'Nothing filed from an idea yet.' : 'Nothing dropped yet.';
}

// The latest digest's line: its date, then any source it could not read.
function sourcesLine(line) {
  const s = data.sources || {};
  const chips = (s.chips || []).filter(function (c) { return !c.ok; });
  line.replaceChildren(document.createTextNode(
    'Digest ' + (data.digest || s.date || 'not read') + (chips.length ? ' · ' : '')));
  chips.forEach(function (c) { line.appendChild(chip(c.text, 'attention', 'board-ideas-source')); });
  line.hidden = false;
}

function paintSegments(root) {
  const counts = { new: data.ideas.length, applied: 0, filtered: 0 };
  data.ledger.forEach(function (r) { counts[r.state] = (counts[r.state] || 0) + 1; });
  root.querySelectorAll('.board-ideas-seg .segmented-item').forEach(function (b) {
    const v = b.dataset.value;
    b.setAttribute('aria-pressed', v === segment ? 'true' : 'false');
    b.textContent = b.textContent.replace(/\s\d+$/, '') + ' ' + counts[v];
  });
  return counts;
}

function paint() {
  const root = card();
  if (!root) return;
  const list = root.querySelector('.board-ideas-list');
  const line = root.querySelector('.board-ideas-line');
  const count = root.querySelector('.board-ideas-count');
  const seg = root.querySelector('.board-ideas-seg');
  const state = data ? data.state : 'missing';
  const empty = state === 'ok' && !data.ideas.length && !data.ledger.length;
  root.hidden = state === 'missing' || empty;
  if (root.hidden) return;
  root.dataset.state = state;
  if (state !== 'ok') {
    count.textContent = '—';
    seg.hidden = true;
    list.replaceChildren();
    line.textContent = 'Ideas unknown: ' + (data.reason || 'the webapp did not answer.');
    line.hidden = false;
    return;
  }
  seg.hidden = false;
  const counts = paintSegments(root);
  count.textContent = String(counts.new);
  const entries = entriesFor(segment);
  const fold = !unfolded[segment] && entries.length - SHOWN_ROWS >= 2;
  const rows = (fold ? entries.slice(0, SHOWN_ROWS) : entries).map(ideaRow);
  if (fold) rows.push(moreRow(entries.length - SHOWN_ROWS));
  if (!entries.length) rows.push(el('li', 'board-ideas-empty muted small', emptyText(segment)));
  list.replaceChildren.apply(list, rows);
  sourcesLine(line);
}

// Each Board render: the chief's run state for an open sheet's send.
export function renderIdeas(body, liveRead) {
  chiefRun = chiefRunState(body, liveRead);
  syncIdeaSheet(chiefRun);
}

// Rides the Board poll; reads at most every IDEAS_POLL_MS. A failed read is
// the unknown line, never an empty card.
export async function pollIdeas() {
  if (inFlight || Date.now() - readAt < IDEAS_POLL_MS) return;
  inFlight = true;
  readAt = Date.now();
  let next;
  try {
    next = await jsonApi('/api/inspiration/ideas');
  } catch (_exc) {
    next = { state: 'unknown', reason: '', ideas: [], ledger: [] };
  } finally {
    inFlight = false;
  }
  const sig = JSON.stringify(next);
  if (data && JSON.stringify(data) === sig) return;
  data = next;
  // An id the ledger now holds needs no "sent" mark any more.
  (data.ledger || []).forEach(function (r) { delete sent[r.id]; });
  paint();
}

export function wireIdeas() {
  wireIdeaSheet();
  const root = card();
  if (!root) return;
  root.querySelector('.board-ideas-seg').addEventListener('click', function (ev) {
    const b = ev.target.closest('.segmented-item');
    if (!b || b.dataset.value === segment) return;
    segment = b.dataset.value;
    paint();
  });
}
