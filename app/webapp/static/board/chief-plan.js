/* The Board's Chief's plan card, drawn under Claude's turn on every
 * renderBoard(). board.js fetches the plan beside /api/board and calls
 * renderChiefPlan(); the answer sheet it opens is board-answers.js and the
 * Clear merged control is plan-prune.js.
 */

import { els, state } from '../state.js';
import { icon } from '../_vendored/icons/icons.js';
import { chip } from '../glance.js';
import { actionRow } from '../action-rows.js';
import { openChiefAnswers, openOwnerAnswers, syncChiefAnswers } from './board-answers.js';
import { agoText, newestDateFirst, plural, shortDate } from '../shared/format.js';
import { answeredKeys, itemKey } from './chief-keys.js';
import { boardRepoFilter, isChiefCard, matchesRepoFilter } from './board-dispatch.js';
import { DESKTOP_BOARD, renderBoard, setLaneException } from '../board.js';
import { clearMergedButton, mergedCount } from './plan-prune.js';
import { CLOSED, NOT_LOADED, bucketIndex, bucketName, groupPlanRows } from './plan-buckets.js';

// The fleet chief records its lanes, its queue and what waits on Roberto in a
// plan file (format v1, fleet-config's `chief_ops.py plan`); the server reads
// it tolerantly (src/chief/plan.py) and this card shows it under Claude's
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

function planAge(updatedAt) {
  const ago = agoText(updatedAt);
  return ago ? 'Updated ' + ago : 'Update time unknown';
}

// Running / not running / unknown are three answers: an unreadable
// session-host can't prove the chief is gone.
export function chiefRunState(body, liveRead) {
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

// A foldable group: the section as a <details> whose heading is the summary,
// so a long list does not push the next group off a phone. `defaultOpen` (a
// boolean or a function of nothing) applies until the reader toggles it, after
// which the choice holds across rebuilds. `opts.track` is the usage-log id of the
// summary; `opts.rows` the list's items, `opts.note` an optional muted line under the
// heading, `opts.after` nodes that follow the list inside the fold, `opts.cls` the
// group's extra classes.
const foldOpen = {};
// The Board's collapse-all / expand-all choice (null until used): the open
// state of every group the reader has not toggled since.
let foldAll = null;

export function setAllFolds(open) {
  Object.keys(foldOpen).forEach(function (key) { delete foldOpen[key]; });
  foldAll = open;
}

export function foldGroup(opts) {
  let open;
  if (Object.prototype.hasOwnProperty.call(foldOpen, opts.key)) open = foldOpen[opts.key];
  else if (foldAll !== null) open = foldAll;
  else open = typeof opts.defaultOpen === 'function' ? opts.defaultOpen() : opts.defaultOpen !== false;
  const wrap = document.createElement('details');
  wrap.className = 'board-plan-group board-plan-fold' + (opts.cls ? ' ' + opts.cls : '');
  wrap.open = open;
  const sum = document.createElement('summary');
  sum.dataset.track = opts.track;
  sum.className = 'board-plan-heading board-plan-fold-summary';
  const label = document.createElement('span');
  label.textContent = opts.heading;
  const mark = document.createElement('span');
  mark.className = 'board-plan-fold-chevron';
  mark.setAttribute('aria-hidden', 'true');
  mark.innerHTML = icon('chevron-right');
  sum.append(label, mark);
  wrap.appendChild(sum);
  if (opts.note) wrap.appendChild(planLine('board-plan-section-note', opts.note));
  const list = document.createElement('ul');
  list.className = 'card-list board-list';
  opts.rows.forEach(function (row) { list.appendChild(row); });
  wrap.appendChild(list);
  (opts.after || []).forEach(function (node) { wrap.appendChild(node); });
  wrap.addEventListener('toggle', function () { foldOpen[opts.key] = wrap.open; });
  return wrap;
}

// Set-aside groups fold shut on a phone and open on the desktop board.
function onDesktop() {
  return !!(window.matchMedia && window.matchMedia(DESKTOP_BOARD).matches);
}

// One plan group named after its Backlog bucket; nothing when empty.
// `opts.track` is the fold's usage-log id (a builder in .fleet.toml).
function planBucketGroup(opts) {
  if (!opts.rows.length) return null;
  return foldGroup({
    key: 'plan:' + opts.bucket,
    track: opts.track,
    heading: bucketName(opts.bucket) + ' (' + opts.rows.length + ')',
    rows: opts.rows.map(queueRow),
    cls: opts.cls,
    defaultOpen: opts.setAside ? onDesktop : true,
  });
}

// The queue's rows under the Backlog's bucket names, one row per issue, so
// each group's count is that bucket's count among planned issues: Queued,
// Blocked and Umbrellas equal the Backlog's, and Waiting on you here is the
// planned part of the Backlog's. Queued shows even when empty, unfiltered.
function bucketedQueue(planned, index, filter) {
  const groups = groupPlanRows(planned, index);
  const rows = function (key) { return groups[key] || []; };
  const nodes = [];
  if (rows('queued').length || !filter) {
    nodes.push(foldGroup({
      key: 'plan:queue',
      track: 'board.plan-queue-fold',
      heading: bucketName('queued') + ' (' + rows('queued').length + ')',
      rows: rows('queued').map(queueRow),
      note: 'Only what the chief has scheduled; the Backlog counts every open issue.',
    }));
  }
  const known = ['queued', 'in_progress', 'waiting', 'blocked', 'umbrella', NOT_LOADED, CLOSED];
  const other = [];
  Object.keys(groups).forEach(function (key) {
    if (known.indexOf(key) === -1) other.push.apply(other, groups[key]);
  });
  [
    planBucketGroup({ bucket: 'in_progress', track: 'board.plan-progress-fold', rows: rows('in_progress') }),
    planBucketGroup({ bucket: 'waiting', track: 'board.plan-waiting-fold', rows: rows('waiting') }),
    planBucketGroup({ bucket: 'blocked', track: 'board.plan-blocked-fold', rows: rows('blocked'),
      cls: 'board-plan-parked', setAside: true }),
    planBucketGroup({ bucket: 'umbrella', track: 'board.plan-umbrella-fold', rows: rows('umbrella'),
      cls: 'board-plan-parked', setAside: true }),
    planBucketGroup({ bucket: NOT_LOADED, track: 'board.plan-not-loaded-fold', rows: rows(NOT_LOADED) }),
    planBucketGroup({ bucket: 'other', track: 'board.plan-other-fold', rows: other, setAside: true }),
    planBucketGroup({ bucket: CLOSED, track: 'board.plan-closed-fold', rows: rows(CLOSED), setAside: true }),
  ].forEach(function (node) { if (node) nodes.push(node); });
  return nodes;
}

// Before the first GitHub fetch the Backlog has no buckets to read, so the
// queue splits by the plan's own status, and says so.
function unbucketedQueue(planned, filter) {
  const queue = planned.filter(function (q) { return !isParked(q); });
  const parked = planned.filter(isParked);
  const nodes = [];
  if (queue.length || !filter) {
    nodes.push(foldGroup({
      key: 'plan:queue',
      track: 'board.plan-queue-fold',
      heading: 'Queue (' + queue.length + ')',
      rows: queue.map(queueRow),
      note: 'Grouped by plan status until the Backlog loads from GitHub.',
    }));
  }
  if (parked.length) {
    nodes.push(foldGroup({
      key: 'plan:parked',
      track: 'board.plan-parked-fold',
      heading: 'Parked (' + parked.length + ')',
      rows: parked.map(queueRow),
      cls: 'board-plan-parked',
      defaultOpen: onDesktop,
    }));
  }
  return nodes;
}

// `shown` is the plan positions the repo filter keeps that no
// device has answered yet: the button counts and opens only those.
function answerButton(plan, run, shown) {
  const n = shown.length;
  const btn = document.createElement('button');
  btn.dataset.track = 'board.plan-answer';
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
export function chevron(glyph, onTap) {
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
    meta: [ref, shortDate(w.asked_at), sent ? 'answered, waiting for the chief' : ''].filter(Boolean).join(' · '),
    density: 'one-line',
    label: 'Answer: ' + title,
    onMain: openSheet,
    inert: sent,
    trailing: sent ? null : chevron('chevron-right', openSheet),
  }).li;
}

// The owner-check sheet from an owner-check row or its group's button
// (owner-checks.js): one group's owner checks (`kind`), never the chief's
// questions or the other group's; `ownerRef` is the item to scroll to.
export function openOwnerAnswersAt(ownerRef, kind) {
  const body = state.board;
  const liveRead = !(body && body.live_sessions && body.live_sessions.available === false);
  openOwnerAnswers(state.chiefPlan, chiefRunState(body, liveRead), ownerRef, kind);
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
  // Each queue row is grouped by its issue's Backlog bucket; null before the
  // Backlog has any.
  const backlog = (body && body.columns && body.columns.backlog) || [];
  const index = backlog.some(function (c) { return typeof c.bucket === 'string'; })
    ? bucketIndex(backlog) : null;
  // Rebuilt only when what it shows changes, not on every 5 s poll.
  const sig = JSON.stringify([plan, run, age, answered, filter, index]);
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
    // Newest asked first, an undated one last; the plan order breaks a tie.
    shown.sort(function (a, b) {
      return newestDateFirst(waiting[a].asked_at, waiting[b].asked_at) || a - b;
    });
    const hidden = waiting.length - shown.length;
    if (shown.length) {
      // Answered from any device: not counted, not asked again.
      const open = shown.filter(function (i) {
        return answered.indexOf(itemKey(waiting[i], i)) === -1;
      });
      const rows = shown.map(function (i) { return questionRow(plan, run, open, i, answered); });
      nodes.push(foldGroup({
        key: 'plan:questions',
        track: 'board.plan-questions-fold',
        heading: 'Questions',
        rows: rows,
        after: open.length ? [answerButton(plan, run, open)] : [],
        cls: 'board-plan-group-waiting',
      }));
    }
    // A pending decision is never invisible: say what the filter hides.
    if (hidden) {
      nodes.push(planLine('board-plan-hidden', hidden + ' more question'
        + (hidden === 1 ? '' : 's') + ' in other projects'));
    }
    const lanes = (plan.lanes || []).filter(function (l) { return planRowShown(l, filter); });
    if (lanes.length) {
      nodes.push(foldGroup({
        key: 'plan:lanes',
        track: 'board.plan-lanes-fold',
        heading: 'Lanes (' + lanes.length + ')',
        rows: lanes.map(planLaneRow),
      }));
    }
    // Unfiltered, the Queued heading shows even when empty, as it always has;
    // a filter that empties it hides it, as the columns do.
    const planned = (plan.queue || []).filter(function (q) { return planRowShown(q, filter); });
    nodes.push.apply(nodes, index ? bucketedQueue(planned, index, filter) : unbucketedQueue(planned, filter));
    const merged = mergedCount(plan);
    if (merged) nodes.push(clearMergedButton(merged));
  }
  host.replaceChildren.apply(host, nodes);
}
