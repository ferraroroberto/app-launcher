/* Owner checks on the Board: issues that wait on the owner.
 *
 * Two kinds, from fleet-config's owner_check labels (GET /api/board's
 * `owner_checks` section): shipped work to validate as a user
 * (`validate-after`) and a step owed before work can start (`before-start`).
 * They sit in Your turn under the live sessions, one group per kind. A row's
 * tap unfolds its own detail in place: why it needs you, what to try, and the
 * links to the issue and its proof. The detail's Answer, like the group's
 * button, opens the one answer sheet (board-answers.js), where this module
 * draws each item's block and reads its answer back.
 *
 * The list is a cached read with its age. Before one has landed it is
 * unknown, never "none"; repos the list could not read make it a lower bound,
 * and the group says so. An item answered Later or Problem keeps its row,
 * marked, and leaves the "needs you" count.
 */

import { els } from '../state.js';
import { chip } from '../glance.js';
import { actionRow } from '../action-rows.js';
import { el } from '../dom-utils.js';
import { askOption } from '../chat/ask-option.js';
import { icon } from '../_vendored/icons/icons.js';
import { terminalJsonApi } from '../webauthn.js';
import { boardRepoFilter, matchesRepoFilter } from './board-dispatch.js';
import { agoText } from './chief-plan.js';
import { calendarDaysFromToday } from '../shared/format.js';
import { plural, renderBoard } from '../board.js';

export const VALIDATE_AFTER = 'validate-after';
export const BEFORE_START = 'before-start';

// Before start blocks work from starting, so it leads.
const GROUPS = [
  { kind: BEFORE_START, heading: 'Before you start it', label: 'Before start' },
  { kind: VALIDATE_AFTER, heading: 'To validate', label: 'Validate' },
];

// Rows per group before "N more"; the backfill can list dozens.
const SHOWN = 3;
let unfolded = {};
// The ref whose detail is open, across the 5 s poll's rebuilds.
let expandedRef = null;

function section(body) {
  return (body && body.owner_checks) || null;
}

function waiting(item) {
  return !item.mark;
}

// What waits on you, fleet-wide: null until a list has landed (unknown, never
// zero), else the unmarked items. A payload without the section (a stubbed
// test body) has none, as a payload without `live_sessions` keeps its render.
export function ownerNeedsCount(body) {
  const sec = section(body);
  if (!sec) return 0;
  if (!sec.available) return null;
  return (sec.items || []).filter(waiting).length;
}

// The items the repo filter shows, the waiting ones first, each kind in
// the list's own order.
export function shownOwnerItems(body) {
  const sec = section(body);
  if (!sec) return [];
  const filter = boardRepoFilter();
  const items = (sec.items || []).filter(function (it) { return matchesRepoFilter(it, filter); });
  return items.filter(waiting).concat(items.filter(function (it) { return !waiting(it); }));
}

function kindLabel(kind) {
  const g = GROUPS.find(function (x) { return x.kind === kind; });
  return g ? g.label : kind;
}

// "waiting 3 d" from the block's `since` date; '' when there is none.
function sinceText(since) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(since || ''));
  if (!m) return '';
  const days = Math.max(0, calendarDaysFromToday(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))));
  return days ? 'waiting ' + days + ' d' : 'since today';
}

function markText(item) {
  if (item.mark === 'later') return 'snoozed';
  if (item.mark === 'problem') return 'problem reported, waiting on a fix';
  return '';
}

// A proof link's label: a PR by its number, an artifact by name, else the host.
function linkLabel(url) {
  const pr = /\/pull\/(\d+)/.exec(url);
  if (pr) return 'PR #' + pr[1];
  if (/claude\.ai\/(code\/)?artifact/.test(url)) return 'Artifact';
  const issue = /\/issues\/(\d+)/.exec(url);
  if (issue) return 'Issue #' + issue[1];
  try {
    return new URL(url).hostname;
  } catch (_) {
    return 'Link';
  }
}

// A reference pill inside a 44px tap target.
function link(url, label) {
  const a = el('a', 'board-owner-link');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  const pill = el('span', 'board-owner-pill');
  pill.innerHTML = icon('link');
  pill.appendChild(document.createTextNode(label));
  a.appendChild(pill);
  return a;
}

// The issue first, then every proof link the block carries (http(s) only).
function links(item) {
  const wrap = el('div', 'board-owner-links');
  if (item.url) wrap.appendChild(link(item.url, 'Issue #' + item.number));
  (item.proof || []).forEach(function (url) {
    if (/^https?:\/\//.test(url)) wrap.appendChild(link(url, linkLabel(url)));
  });
  return wrap;
}

// Why it needs you and what to try; a label with no readable block says so.
function details(item, cls) {
  const nodes = [];
  if (!item.block) {
    nodes.push(el('p', cls, 'Details unknown: the issue carries the label but no readable owner-check note. Open the issue to see what it needs.'));
    return nodes;
  }
  if (item.reason) nodes.push(labelled(cls, 'Why', item.reason));
  if (item.try) nodes.push(labelled(cls, 'Try', item.try));
  return nodes;
}

function labelled(cls, label, text) {
  const p = el('p', cls);
  p.appendChild(el('strong', null, label + ': '));
  p.appendChild(document.createTextNode(text));
  return p;
}

// ---------------------------------------------------------------- the lane

function itemRow(item, onAnswer) {
  const expanded = expandedRef === item.ref;
  const title = item.title || item.ref;
  const meta = [item.ref, markText(item) || sinceText(item.since)].filter(Boolean).join(' · ');
  const chips = item.block ? [] : [chip('details unknown', 'attention', 'board-owner-unknown')];
  const row = actionRow({
    className: 'board-item board-owner-row' + (waiting(item) ? '' : ' is-answered'),
    title: title,
    meta: meta,
    chips: chips,
    label: (expanded ? 'Hide' : 'Show') + ' what ' + item.ref + ' needs',
    onMain: function () {
      expandedRef = expanded ? null : item.ref;
      renderBoard();
    },
  });
  row.li.dataset.ref = item.ref;
  row.main.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  if (!expanded) return row.li;
  row.li.classList.add('expanded');
  const detail = el('div', 'board-owner-detail');
  detail.id = 'owner-detail-' + item.ref.replace(/[^A-Za-z0-9_-]/g, '-');
  row.main.setAttribute('aria-controls', detail.id);
  details(item, 'board-owner-line').forEach(function (n) { detail.appendChild(n); });
  detail.appendChild(links(item));
  const answer = el('button', 'button-tint board-owner-answer', 'Answer');
  answer.type = 'button';
  answer.addEventListener('click', function () { onAnswer(item.ref); });
  detail.appendChild(answer);
  row.li.appendChild(detail);
  return row.li;
}

function moreRow(kind, n) {
  const unfold = function () {
    unfolded[kind] = true;
    renderBoard();
  };
  return actionRow({
    className: 'board-item board-owner-more',
    title: plural(n, 'more', 'more'),
    label: 'Show ' + n + ' more',
    density: 'one-line',
    onMain: unfold,
  }).li;
}

function group(g, items, onAnswer) {
  const wrap = el('div', 'board-plan-group board-owner-group');
  wrap.dataset.kind = g.kind;
  wrap.appendChild(el('h4', 'board-plan-heading', g.heading + ' (' + items.length + ')'));
  const folded = !unfolded[g.kind] && items.length > SHOWN + 1;
  const listed = folded ? items.slice(0, SHOWN) : items;
  // The open detail's row stays listed, folded or not.
  if (folded && expandedRef && listed.every(function (it) { return it.ref !== expandedRef; })) {
    const open = items.find(function (it) { return it.ref === expandedRef; });
    if (open) listed.push(open);
  }
  const list = el('ul', 'card-list board-list');
  listed.forEach(function (it) { list.appendChild(itemRow(it, onAnswer)); });
  if (folded) list.appendChild(moreRow(g.kind, items.length - listed.length));
  wrap.appendChild(list);
  return wrap;
}

// The list's freshness and gaps, said every time: its age, a failed
// refresh, unreadable repos. Never a bare empty list.
function stateLine(sec) {
  if (!sec.available && !sec.error && !sec.refreshing) {
    return 'Owner checks not loaded yet. Refresh reads them.';
  }
  if (!sec.available) {
    return sec.refreshing ? 'Reading owner checks…' : 'Owner checks unknown: the list could not be read.';
  }
  const parts = ['Owner checks updated ' + (agoText(sec.fetched_at) || 'at an unknown time')];
  if (sec.refreshing) parts.push('refreshing');
  if (sec.error) parts.push('the last refresh failed');
  const unknown = (sec.unknown || []).length;
  if (unknown) parts.push(plural(unknown, 'repo', 'repos') + ' unreadable, so this list may be short');
  return parts.join(' · ');
}

// Fills Your turn's owner block; returns how many waiting items it shows, for
// the lane's count. `onAnswer(ref)` opens the answer sheet on that item.
export function renderOwnerChecks(body, onAnswer) {
  const host = els.boardColumns && els.boardColumns.querySelector('.board-owner');
  if (!host) return 0;
  const sec = section(body);
  if (!sec) {
    host.dataset.sig = '';
    host.replaceChildren();
    return 0;
  }
  const items = shownOwnerItems(body);
  const waitingCount = items.filter(waiting).length;
  const sig = JSON.stringify([sec && sec.available, sec && sec.fetched_at, sec && sec.error,
    sec && sec.refreshing, sec && sec.unknown, items, expandedRef, unfolded,
    agoText(sec && sec.fetched_at), boardRepoFilter()]);
  if (host.dataset.sig === sig) return waitingCount;
  host.dataset.sig = sig;
  const nodes = [];
  GROUPS.forEach(function (g) {
    const mine = items.filter(function (it) { return it.kind === g.kind; });
    if (mine.length) nodes.push(group(g, mine, onAnswer));
  });
  if (waitingCount) {
    const btn = el('button', 'button-tint board-plan-answer board-owner-answer-all');
    btn.type = 'button';
    btn.innerHTML = icon('messages-square') + ' ';
    btn.appendChild(document.createTextNode('Answer ' + plural(waitingCount, 'owner check', 'owner checks')));
    const first = items.find(waiting);
    btn.addEventListener('click', function () { onAnswer(first.ref); });
    nodes.push(btn);
  }
  const line = el('p', 'board-plan-line muted small board-owner-state', stateLine(sec));
  if (sec && sec.error) line.title = sec.error;
  nodes.push(line);
  host.replaceChildren.apply(host, nodes);
  return waitingCount;
}

// ---------------------------------------------------------------- the sheet

const OPTIONS = {
  [VALIDATE_AFTER]: [
    { answer: 'validated', label: 'Validated', description: 'It works: removes the label and notes it on the issue' },
    { answer: 'later', label: 'Later', description: 'Snooze it until tomorrow' },
  ],
  [BEFORE_START]: [
    { answer: 'done', label: 'Done', description: 'The step is done: removes the label so the work can start' },
    { answer: 'later', label: 'Later', description: 'Snooze it until tomorrow' },
  ],
};

export function emptyOwnerDraft() {
  return { pick: null, problem: '' };
}

export function ownerSheetKey(item) {
  return 'owner:' + item.ref;
}

// One item's answer, or null: a typed problem wins over a pick.
export function ownerAnswerOf(item, draft) {
  const text = String((draft && draft.problem) || '').split(/\s+/).filter(Boolean).join(' ');
  if (item.kind === VALIDATE_AFTER && text) {
    return { ref: item.ref, kind: item.kind, answer: 'problem', text: text };
  }
  if (draft && draft.pick) return { ref: item.ref, kind: item.kind, answer: draft.pick };
  return null;
}

// The line the chief reads for an applied answer; null for a snooze, which
// the chief has nothing to do about.
export function ownerMessageLine(item, ans) {
  const what = item.title ? ' ' + item.title : '';
  if (ans.answer === 'validated') return '[' + item.ref + '] Validated:' + what;
  if (ans.answer === 'done') return '[' + item.ref + '] Before-start step done:' + what;
  if (ans.answer === 'problem') {
    return '[' + item.ref + '] Problem found validating' + what + ': ' + ans.text + ' (please file a fix)';
  }
  return null;
}

function paint(block, draft) {
  block.querySelectorAll('.tr-ask-opt').forEach(function (b) {
    b.setAttribute('aria-pressed', draft.pick === b.dataset.answer ? 'true' : 'false');
  });
}

// One owner check in the answer sheet: its kind, repo and ref, the title as
// the question, why and what to try, its links, Validated / Done and Later,
// and (to validate) a Problem field. `decorate(input, onInput)` adds the
// field's dictation, which the sheet owns.
export function ownerSheetBlock(item, draft, onChange, decorate) {
  const block = el('section', 'tr-ask chief-answer owner-answer');
  block.dataset.key = ownerSheetKey(item);
  const head = el('div', 'tr-ask-head chief-answer-head');
  head.appendChild(el('span', 'tr-ask-header', kindLabel(item.kind)));
  head.appendChild(el('span', 'tr-ask-header chief-answer-repo', item.repo));
  const ref = el('a', 'chief-answer-ref', item.ref);
  ref.href = item.url;
  ref.target = '_blank';
  ref.rel = 'noopener';
  head.appendChild(ref);
  block.appendChild(head);
  block.appendChild(el('p', 'tr-ask-question', item.title || item.ref));
  details(item, 'tr-ask-hint owner-answer-line').forEach(function (n) { block.appendChild(n); });
  block.appendChild(links(item));

  let input = null;
  const list = el('div', 'tr-ask-options');
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', 'Answer ' + item.ref);
  OPTIONS[item.kind].forEach(function (opt, i) {
    const b = askOption({ n: i + 1, label: opt.label, description: opt.description, mark: true });
    b.dataset.answer = opt.answer;
    b.addEventListener('click', function () {
      draft.pick = draft.pick === opt.answer ? null : opt.answer;
      // One answer per item: a pick replaces a typed problem.
      if (draft.pick && input) {
        draft.problem = '';
        input.value = '';
      }
      paint(block, draft);
      onChange();
    });
    list.appendChild(b);
  });
  block.appendChild(list);

  if (item.kind === VALIDATE_AFTER) {
    input = el('input', 'tr-ask-input');
    input.type = 'text';
    input.maxLength = 2000;
    input.placeholder = 'Problem: what went wrong';
    input.setAttribute('aria-label', 'Problem found in ' + item.ref);
    input.value = draft.problem;
    const typed = function () {
      draft.problem = input.value;
      if (input.value.trim() && draft.pick) {
        draft.pick = null;
        paint(block, draft);
      }
      onChange();
    };
    input.addEventListener('input', typed);
    const other = el('div', 'tr-ask-other');
    other.appendChild(input);
    if (decorate) decorate(other, input, typed);
    block.appendChild(other);
  }
  paint(block, draft);
  return block;
}

// POST the sheet's owner-check answers (passkey-gated: GitHub writes);
// resolves to `{results: [{ref, ok, error?}], owner_checks}`.
export function postOwnerAnswers(answers) {
  return terminalJsonApi('/api/board/owner-checks/answer', { method: 'POST', body: { answers: answers } });
}
