/* The Board's "Since you last looked" card: the chief's journal, new entries
 * first.
 *
 * GET /api/board/chief-journal serves the entries newest first, each flagged
 * `new` against Roberto's last look; the chief is the journal's only writer, so
 * the card only reads. The last look moves when he sends the chief a message
 * from the Board or submits the answer sheet: journal-look.js stamps it on the
 * server (every device agrees) and the card re-reads at once.
 *
 * Quiet by contract: no journal file keeps the card hidden, a file that
 * cannot be read is one muted line, never a toast. No new entry collapses
 * the card to one line and a "Show all" button, which also reveals the older
 * entries beside the new ones. The read rides the Board poll but runs at most
 * every JOURNAL_POLL_MS.
 */

import { jsonApi } from '../api.js';
import { actionRow } from '../action-rows.js';
import { chip } from '../glance.js';
import { el } from '../dom-utils.js';
import { agoText } from '../shared/format.js';
import { chevron } from './chief-plan.js';
import { onLooked } from './journal-look.js';

const JOURNAL_POLL_MS = 10 * 1000;

// Kind -> chip tone; a kind the writer adds later reads as neutral.
const KIND_TONES = {
  merged: 'accent', dispatched: 'accent', asked: 'attention', failed: 'danger',
};

let data = null;
let readAt = 0;
let inFlight = false;
let showAll = false;
let paintedSig = '';

function card() { return document.getElementById('boardJournal'); }

function journalRow(entry) {
  const open = entry.ref_url
    ? function () { window.open(entry.ref_url, '_blank', 'noopener'); }
    : null;
  const row = actionRow({
    className: 'board-item board-journal-row' + (open ? ' is-link' : '') + (entry.new ? '' : ' is-read'),
    title: entry.text || entry.ref || entry.kind,
    meta: [agoText(entry.ts), entry.ref].filter(Boolean).join(' · '),
    chips: [chip(entry.kind, KIND_TONES[entry.kind] || 'neutral', 'board-journal-kind')],
    label: 'Open ' + (entry.ref || 'the entry') + ' on GitHub',
    onMain: open,
    inert: !open,
    trailing: open ? chevron('chevron-right', open) : null,
  });
  if (open) row.main.dataset.track = 'board.journal-open';
  return row.li;
}

function showAllButton(total) {
  const b = el('button', 'button-ghost board-journal-all', showAll ? 'Show new only' : 'Show all ' + total);
  b.type = 'button';
  b.dataset.track = 'board.journal-show-all';
  b.addEventListener('click', function () {
    showAll = !showAll;
    paint(true);
  });
  return b;
}

function quietLine(text) { return el('p', 'board-journal-line muted small', text); }

function paint(force) {
  const root = card();
  if (!root) return;
  const state = data ? data.state : 'empty';
  // The age text moves with the clock, so the minute is part of the picture.
  const sig = JSON.stringify([data, showAll, Math.floor(Date.now() / 60000)]);
  if (!force && sig === paintedSig) return;
  paintedSig = sig;
  root.hidden = state === 'empty' || !data;
  if (root.hidden) return;
  root.dataset.state = state;
  const count = root.querySelector('.board-journal-count');
  const body = root.querySelector('.board-journal-body');
  if (state !== 'ok') {
    count.textContent = '';
    delete root.dataset.quiet;
    body.replaceChildren(quietLine('Journal unreadable.'));
    return;
  }
  const entries = data.entries;
  const shown = showAll ? entries : entries.filter(function (e) { return e.new; });
  count.textContent = data.new_count ? String(data.new_count) : '';
  if (!shown.length) {
    // One line: nothing new, with the way to the older entries beside it.
    root.dataset.quiet = 'true';
    const line = el('div', 'board-journal-quiet');
    line.append(quietLine('Nothing new since you last looked.'), showAllButton(entries.length));
    body.replaceChildren(line);
    return;
  }
  delete root.dataset.quiet;
  const list = el('ul', 'card-list board-journal-list');
  list.append.apply(list, shown.map(journalRow));
  const nodes = [list];
  if (data.new_count !== entries.length) nodes.push(showAllButton(entries.length));
  body.replaceChildren.apply(body, nodes);
}

// Rides the Board poll. A failed read keeps what was shown: the card is
// quiet, and a stale entry list is better than a blank one.
export async function pollJournal() {
  if (inFlight || Date.now() - readAt < JOURNAL_POLL_MS) return;
  inFlight = true;
  readAt = Date.now();
  let next = null;
  try {
    next = await jsonApi('/api/board/chief-journal');
  } catch (_exc) {
    next = null;
  } finally {
    inFlight = false;
  }
  if (next) data = next;
  paint(false);
}

// The stamp landed (journal-look.js): read again at once, so the entries it
// just counted as seen leave the card without waiting for the next poll.
export function wireJournal() {
  onLooked(function () {
    readAt = 0;
    return pollJournal();
  });
}
