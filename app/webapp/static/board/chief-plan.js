/* The Board's Chief's plan card, drawn under Claude's turn on every
 * renderBoard(). board.js fetches the plan beside /api/board and calls
 * renderChiefPlan(); the answer sheet it opens is board-answers.js and the
 * Clear merged control is plan-prune.js.
 */

import { els, state } from '../state.js';
import { icon } from '../_vendored/icons/icons.js';
import { chip } from '../glance.js';
import { actionRow } from '../action-rows.js';
import { openChiefAnswers, syncChiefAnswers } from '../board-answers.js';
import { answeredKeys, itemKey } from '../chief-keys.js';
import { boardRepoFilter, isChiefCard, matchesRepoFilter } from '../board-dispatch.js';
import { DESKTOP_BOARD, plural, renderBoard, setLaneException } from '../board.js';
import { clearMergedButton, mergedCount } from './plan-prune.js';

// The fleet chief records its lanes, its queue and what waits on Roberto in a
// plan file (format v1, fleet-config's `chief_ops.py plan`); the server reads
// it tolerantly (src/chief_plan.py) and this card shows it under Claude's
// turn. No plan, the card stays blank. A plan the server could not trust adds
// one quiet note, never a toast. Whether the chief is running comes from the
// session cards /api/board already carries, and is said on its own line: a
// plan with no chief behind it must not read as current. What waits on
// Roberto carries an "Answer N questions" button into the one-shot answer
// sheet (board-answers.js).

// Status -> chip tone, on the tone map: work in progress is accent,
// waiting on you is attention, merged and anything unlisted (an unknown status
// included) is neutral.
const PLAN_TONES = {
  building: 'accent', gate: 'accent',
  waiting: 'attention', 'waiting-roberto': 'attention',
  merged: 'neutral',
};

// "just now" / "12 min ago" / "3 h ago" / "2 d ago", or '' for a time that
// can't be read: the plan's age and the Backlog footer's.
export function agoText(iso) {
  const t = Date.parse(iso || '');
  if (isNaN(t)) return '';
  const min = Math.floor((Date.now() - t) / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return min + ' min ago';
  const h = Math.floor(min / 60);
  if (h < 48) return h + ' h ago';
  return Math.floor(h / 24) + ' d ago';
}

function planAge(updatedAt) {
  const ago = agoText(updatedAt);
  return ago ? 'Updated ' + ago : 'Update time unknown';
}

// Running / not running / unknown are three answers: an unreadable
// session-host can't prove the chief is gone.
function chiefRunState(body, liveRead) {
  const columns = (body && body.columns) || {};
  const running = Object.keys(columns).some(function (key) {
    return (columns[key] || []).some(function (c) {
      return isChiefCard(c) && c.alive !== false;
    });
  });
  if (running) return 'running';
  return liveRead ? 'stopped' : 'unknown';
}

function planLine(cls, text) {
  const p = document.createElement('p');
  p.className = 'board-plan-line muted small' + (cls ? ' ' + cls : '');
  p.textContent = text;
  return p;
}

// A plan section: its heading carries the row count, "Lanes (2)", and
// an optional muted line under it (`note`) says what the number counts.
function planGroup(heading, rows, cls, note) {
  const wrap = document.createElement('div');
  wrap.className = 'board-plan-group' + (cls ? ' ' + cls : '');
  const h = document.createElement('h4');
  h.className = 'board-plan-heading';
  h.textContent = heading;
  wrap.appendChild(h);
  if (note) wrap.appendChild(planLine('board-plan-section-note', note));
  const list = document.createElement('ul');
  list.className = 'card-list board-list';
  rows.forEach(function (row) { list.appendChild(row); });
  wrap.appendChild(list);
  return wrap;
}

// Parked: the same section as a <details>, so a long parked list does
// not push the queue off a phone. Collapsed by default on a phone, open on the
// desktop board; once the reader toggles it the choice holds across rebuilds.
let parkedOpen = null;

function parkedGroup(rows) {
  const open = parkedOpen === null
    ? !!(window.matchMedia && window.matchMedia(DESKTOP_BOARD).matches)
    : parkedOpen;
  const wrap = document.createElement('details');
  wrap.className = 'board-plan-group board-plan-parked';
  wrap.open = open;
  const sum = document.createElement('summary');
  sum.className = 'board-plan-heading board-plan-parked-summary';
  const label = document.createElement('span');
  label.textContent = 'Parked (' + rows.length + ')';
  const mark = document.createElement('span');
  mark.className = 'board-plan-parked-chevron';
  mark.setAttribute('aria-hidden', 'true');
  mark.innerHTML = icon('chevron-right');
  sum.append(label, mark);
  const list = document.createElement('ul');
  list.className = 'card-list board-list';
  rows.forEach(function (row) { list.appendChild(row); });
  wrap.append(sum, list);
  wrap.addEventListener('toggle', function () { parkedOpen = wrap.open; });
  return wrap;
}

// `shown` is the plan positions the repo filter keeps that no
// device has answered yet: the button counts and opens only those.
function answerButton(plan, run, shown) {
  const n = shown.length;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'button-tint board-plan-answer';
  btn.innerHTML = icon('messages-square') + ' ';
  btn.appendChild(document.createTextNode('Answer ' + n + (n === 1 ? ' question' : ' questions')));
  btn.addEventListener('click', function () { openChiefAnswers(plan, run, shown); });
  return btn;
}

// The Board's repo filter narrows the plan too, so a filtered Board
// is one project's whole picture. A row's repo is its own `repo`, else the
// repo half of a `repo#N` ref. A row with neither is fleet-wide context and
// stays under any filter: hiding it could hide a question that needs an
// answer.
const PLAN_REF_REPO = /^([A-Za-z0-9._-]+)#\d+$/;

function planRowRepo(row) {
  if (row.repo) return row.repo;
  const m = PLAN_REF_REPO.exec(row.ref || '');
  return m ? m[1] : null;
}

function planRowShown(row, filter) {
  const repo = planRowRepo(row);
  return !repo || matchesRepoFilter({ repo: repo }, filter);
}

// What the chief has set aside: shown under Parked, never in the
// queue. Every other status is work in the queue's order.
const PARKED_STATUSES = ['parked', 'waiting-roberto'];

function isParked(q) {
  return PARKED_STATUSES.indexOf(q.status) !== -1;
}

// A queue row: the issue title leads, the meta is `repo#N`, then the
// chief's note. An item without a title is named by its label and keeps just
// the note here.
function queueRow(q) {
  const ref = q.ref && q.ref.charAt(0) === '#' && q.repo ? q.repo + q.ref : q.ref;
  const label = ref || q.repo;
  const meta = [];
  if (q.title && label) meta.push(label);
  if (q.note) meta.push(q.note);
  return planRow({
    title: q.title || label,
    meta: meta,
    status: q.status,
    model: q.model,
    url: q.ref_url,
    ref: label,
  });
}

// A lane: the repo is its title, the item (`#N`) its meta.
function planLaneRow(l) {
  return planRow({
    title: l.repo || 'lane',
    meta: l.item ? [l.item] : [],
    status: l.status,
    model: l.model,
    url: l.ref_url,
    ref: l.item ? (l.repo || '') + l.item : '',
  });
}

// A lane, queue or parked row (on the shared action-row as the Backlog
// card's rows are): line 1 the title, on its own so it reads in full; line 2
// the meta pieces joined by " · " with the chief's optional model word
// among them, the status chip at the right end of that line. A row with a
// link (`url`, resolved by the reader from its `repo#N`) is a tap that opens
// it and carries the trailing chevron that says so; a row without one reports
// and does nothing.
function planRow(o) {
  const pieces = o.meta.slice();
  if (o.model) {
    const tag = document.createElement('span');
    tag.className = 'board-plan-model';
    tag.textContent = o.model.charAt(0).toUpperCase() + o.model.slice(1);
    pieces.splice(Math.min(1, pieces.length), 0, tag);
  }
  const open = o.url ? function () { window.open(o.url, '_blank', 'noopener'); } : null;
  const row = actionRow({
    className: 'board-item board-plan-row' + (open ? ' is-link' : ''),
    title: o.title,
    meta: pieces.length ? ' ' : '',
    chips: o.status ? [chip(o.status, PLAN_TONES[o.status] || 'neutral', 'board-plan-chip')] : [],
    label: 'Open ' + (o.ref || o.title) + ' on GitHub',
    onMain: open,
    inert: !open,
    trailing: open ? chevron('chevron-right', open) : null,
  });
  // The pieces take the place of the meta text, so the line still
  // ellipsizes as one and the chip never does.
  const text = row.meta && row.meta.querySelector('.action-row-meta-text');
  if (text) {
    text.textContent = '';
    pieces.forEach(function (p, i) {
      if (i) text.appendChild(document.createTextNode(' · '));
      text.appendChild(typeof p === 'string' ? document.createTextNode(p) : p);
    });
  }
  return row.li;
}

// A row's trailing chevron (design.md action-row: a row that opens a sheet
// carries one). It sits outside the main button, so it takes the same tap.
function chevron(glyph, onTap) {
  const el = document.createElement('span');
  el.className = 'board-plan-chevron';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = icon(glyph);
  el.addEventListener('click', onTap);
  return el;
}

// The questions still waiting on you that the repo filter shows, unanswered:
// the folded plan card's "N questions for you".
function pendingQuestions(plan, answered, filter) {
  if (plan.state !== 'ok') return 0;
  return (plan.waiting_on_roberto || []).filter(function (w, i) {
    return planRowShown(w, filter) && answered.indexOf(itemKey(w, i)) === -1;
  }).length;
}

// The first QUESTIONS_SHOWN questions, then one "N more"
// row that unfolds the rest in place (for as long as the page lives), then
// the one Answer button.
const QUESTIONS_SHOWN = 3;
let questionsUnfolded = false;

// One waiting question: its one-line text (the sheet asks the full
// question), its ref as the meta, a chevron; a tap opens the answer sheet on
// it. One already answered says so and does nothing on tap: the sheet skips
// it.
function questionRow(plan, run, open, i, answered) {
  const w = plan.waiting_on_roberto[i];
  const sent = answered.indexOf(itemKey(w, i)) !== -1;
  const title = w.text || w.question || w.ref;
  const ref = (w.text || w.question) ? (w.ref || w.repo) : '';
  const openSheet = function () { openChiefAnswers(plan, run, open, i); };
  return actionRow({
    className: 'board-item board-plan-row board-plan-waiting' + (sent ? ' is-answered' : ''),
    title: title,
    meta: [ref, sent ? 'answered, waiting for the chief' : ''].filter(Boolean).join(' · '),
    density: 'one-line',
    label: 'Answer: ' + title,
    onMain: openSheet,
    inert: sent,
    trailing: sent ? null : chevron('chevron-right', openSheet),
  }).li;
}

function moreRow(n) {
  const unfold = function () {
    questionsUnfolded = true;
    renderBoard();
  };
  const text = plural(n, 'more question', 'more questions');
  return actionRow({
    className: 'board-item board-plan-more',
    title: text,
    label: 'Show ' + text,
    density: 'one-line',
    onMain: unfold,
    trailing: chevron('chevron-down', unfold),
  }).li;
}

export function renderChiefPlan(body, liveRead) {
  const host = els.boardColumns && els.boardColumns.querySelector('.board-plan-body');
  if (!host) return;
  const plan = state.chiefPlan || { state: 'empty' };
  const run = plan.state === 'ok' ? chiefRunState(body, liveRead) : '';
  const age = plan.state === 'ok' ? planAge(plan.updated_at) : '';
  // An open answer sheet follows whether the chief runs, every poll — read
  // from the sessions, whatever state the plan itself is in.
  syncChiefAnswers(chiefRunState(body, liveRead));
  const answered = plan.state === 'ok' ? answeredKeys(plan) : [];
  const filter = boardRepoFilter();
  const pending = pendingQuestions(plan, answered, filter);
  setLaneException(els.boardChiefPlan,
    pending ? plural(pending, 'question', 'questions') + ' for you' : '',
    'attention');
  // Rebuilt only when what it shows changes, not on every 5 s poll.
  const sig = JSON.stringify([plan, run, age, answered, filter, questionsUnfolded]);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  host.dataset.state = plan.state;
  const nodes = [];
  if (plan.state === 'unreadable') {
    nodes.push(planLine('board-plan-note', 'Plan unreadable.'));
  } else if (plan.state === 'unavailable') {
    nodes.push(planLine('board-plan-note', 'Plan not loaded — the webapp did not answer.'));
  } else if (plan.state === 'ok') {
    if (run === 'stopped') {
      nodes.push(planLine('board-plan-chief', 'Chief not running — this plan may be out of date.'));
    } else if (run === 'unknown') {
      nodes.push(planLine('board-plan-chief', 'Chief status unknown — session-host unreachable.'));
    }
    nodes.push(planLine('board-plan-age', age));
    const waiting = plan.waiting_on_roberto || [];
    // Positions, not items: an answer's key is its place in the whole list.
    const shown = [];
    waiting.forEach(function (w, i) { if (planRowShown(w, filter)) shown.push(i); });
    const hidden = waiting.length - shown.length;
    if (shown.length) {
      // Answered from any device: not counted, not asked again.
      const open = shown.filter(function (i) {
        return answered.indexOf(itemKey(waiting[i], i)) === -1;
      });
      // "1 more" would take a row to save one: fold only from two hidden.
      const folded = !questionsUnfolded && shown.length > QUESTIONS_SHOWN + 1;
      const listed = folded ? shown.slice(0, QUESTIONS_SHOWN) : shown;
      const rows = listed.map(function (i) { return questionRow(plan, run, open, i, answered); });
      if (folded) rows.push(moreRow(shown.length - listed.length));
      const group = planGroup('Waiting on you', rows, 'board-plan-group-waiting');
      if (open.length) group.appendChild(answerButton(plan, run, open));
      nodes.push(group);
    }
    // A pending decision is never invisible: say what the filter hides.
    if (hidden) {
      nodes.push(planLine('board-plan-hidden', hidden + ' more question'
        + (hidden === 1 ? '' : 's') + ' in other projects'));
    }
    const lanes = (plan.lanes || []).filter(function (l) { return planRowShown(l, filter); });
    if (lanes.length) {
      nodes.push(planGroup('Lanes (' + lanes.length + ')', lanes.map(planLaneRow)));
    }
    // Unfiltered, the Queue heading shows even when empty, as it always has;
    // a filter that empties it hides it, as the columns do. Parked items
    // leave the queue for their own section below it.
    const planned = (plan.queue || []).filter(function (q) { return planRowShown(q, filter); });
    const queue = planned.filter(function (q) { return !isParked(q); });
    const parked = planned.filter(isParked);
    if (queue.length || !filter) {
      nodes.push(planGroup('Queue (' + queue.length + ')', queue.map(queueRow), '',
        'Only what the chief has scheduled; the Backlog counts every open issue.'));
    }
    if (parked.length) nodes.push(parkedGroup(parked.map(queueRow)));
    const merged = mergedCount(plan);
    if (merged) nodes.push(clearMergedButton(merged));
  }
  host.replaceChildren.apply(host, nodes);
}
