/* The Done lane's earlier days: today stays as /api/board serves it; "Show
 * earlier" adds one more local day per tap (yesterday, then the day before, up
 * to a week) from GET /api/board/done, under day headers. The days are read on
 * tap only and kept here until the page reloads, never in the 5 s poll.
 */

import { els } from '../state.js';
import { apiFailToast, jsonApi } from '../api.js';
import { boardRepoFilter, matchesRepoFilter } from './board-dispatch.js';
import { renderBoard, renderDoneCard } from '../board.js';

// The server's cap (app/webapp/routers/board.py::_DONE_DAYS_BACK_MAX).
const MAX_DAYS_BACK = 7;

// Loaded days, newest first: { day: 'YYYY-MM-DD', daysBack, items }.
let earlier = [];
let loading = false;
// What the earlier-days block was last built from; the poll re-renders every
// 5 s and must not replace rows (and a focused one) when nothing changed.
let builtSig = '';

// The server's `day` is a local calendar date, so it is read as one: building
// a Date from its parts keeps it on that day, where Date.parse('YYYY-MM-DD')
// would read UTC midnight and drift a day back west of Greenwich.
function dayLabel(group) {
  if (group.daysBack === 1) return 'Yesterday';
  const [y, m, d] = group.day.split('-').map(Number);
  return new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })
    .format(new Date(y, m - 1, d));
}

function groupEl(group, items) {
  const section = document.createElement('section');
  section.className = 'board-day';
  section.dataset.day = group.day;
  const head = document.createElement('h4');
  head.className = 'board-day-head';
  head.textContent = dayLabel(group);
  section.append(head);
  if (!items.length) {
    const none = document.createElement('p');
    none.className = 'board-day-none muted small';
    none.textContent = 'Nothing closed.';
    section.append(none);
    return section;
  }
  const list = document.createElement('ul');
  list.className = 'card-list board-list board-list-earlier';
  list.replaceChildren(...items.map(function (card) {
    return renderDoneCard(card, new Date(card.closed_at || card.updated_at));
  }));
  section.append(list);
  return section;
}

export function doneEarlierLoaded() {
  return earlier.length > 0;
}

async function showEarlier() {
  if (loading || earlier.length >= MAX_DAYS_BACK) return;
  loading = true;
  renderBoard();
  try {
    const daysBack = earlier.length + 1;
    const body = await jsonApi('/api/board/done?days_back=' + daysBack);
    earlier.push({ day: body.day, daysBack: daysBack, items: body.items || [] });
  } catch (exc) {
    apiFailToast('Could not read the earlier day', exc);
  } finally {
    loading = false;
    renderBoard();
  }
}

// Called by every renderBoard(): the "Today" header and the earlier days once
// any are loaded, and the Show earlier button while there is a day left to
// read from a GitHub cache that has been loaded.
export function renderDoneEarlier(ghLoaded) {
  const head = els.boardColumns.querySelector('.board-day-head[data-col="done"]');
  const block = els.boardColumns.querySelector('.board-done-earlier');
  const more = els.boardColumns.querySelector('.board-done-more');
  if (!head || !block || !more) return;
  head.hidden = earlier.length === 0;

  const filter = boardRepoFilter();
  const groups = earlier.map(function (group) {
    return {
      group: group,
      items: group.items.filter(function (card) { return matchesRepoFilter(card, filter); }),
    };
  });
  const sig = JSON.stringify(groups.map(function (g) {
    return [g.group.day, g.items.map(function (c) { return [c.repo, c.number, c.closed_at, c.title]; })];
  }));
  if (sig !== builtSig) {
    builtSig = sig;
    block.replaceChildren(...groups.map(function (g) { return groupEl(g.group, g.items); }));
  }

  more.hidden = !ghLoaded || earlier.length >= MAX_DAYS_BACK;
  more.disabled = loading;
  more.textContent = loading ? 'Reading…' : 'Show earlier';
  if (!more.dataset.wired) {
    more.dataset.wired = '1';
    more.addEventListener('click', showEarlier);
  }
}
