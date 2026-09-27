/* The chief's answer sheet (issue #1295).
 *
 * The Chief's-plan card (#1279, board.js) lists what waits on Roberto. This
 * module is the one-shot way to answer all of it: a sheet in the style of the
 * transcript's AskUserQuestion card (#1149, the same `.tr-ask-*` rows) with
 * one block per waiting item, in file order: the repo and ref, the question,
 * the detail, the chief's recommendation, the options (single- or
 * multi-select), an Other field on every item, and an "Anything else" box
 * under them all. Done composes one plain-text message and sends it through
 * the path the chat bar already uses (board-dispatch.js::sendToChief), so the
 * sheet adds no second way into the chief's PTY.
 *
 * The app never writes the plan file: the chief removes the items it has
 * taken. Until the plan's `updated_at` moves, this viewer's browser marks the
 * items it sent as "answered, waiting for the chief" (localStorage when it
 * works, memory otherwise, so the page never depends on it).
 *
 * The sheet is built when it opens and never by the 5 s poll, so a poll
 * can't wipe an answer being typed; the poll only re-gates Done
 * (syncChiefAnswers) on whether the chief is running.
 */

import { els } from './state.js';
import { apiFailToast, toast } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { startWorkTimer } from './voice.js';
import { sendToChief } from './board-dispatch.js';
import { renderBoard } from './board.js';

const ANSWERED_KEY = 'launcher.chiefAnswered';
const DONE_LABEL = 'Done';

// The open sheet: the plan it was built from, and one draft per item
// ({ picks: [option index], other: '' }).
let sheet = null;
// Whether the chief is running, as the Board last read it.
let chiefRun = '';
let sending = false;

// ------------------------------------------------------ answered marks

let marks = null;

function loadMarks() {
  if (marks) return marks;
  marks = { updated_at: null, ids: [] };
  try {
    const raw = JSON.parse(localStorage.getItem(ANSWERED_KEY) || 'null');
    if (raw && typeof raw.updated_at === 'string' && Array.isArray(raw.ids)) {
      marks = { updated_at: raw.updated_at, ids: raw.ids.map(String) };
    }
  } catch (_) { /* no storage: marks live in memory only */ }
  return marks;
}

// The item's key: the chief's own id, else its position in the list.
export function itemKey(item, index) {
  return item.id || String(index);
}

// The keys this viewer answered on this version of the plan.
export function answeredKeys(plan) {
  const m = loadMarks();
  if (!plan || m.updated_at !== (plan.updated_at || '')) return [];
  return m.ids;
}

function markAnswered(plan, keys) {
  const kept = answeredKeys(plan);
  marks = {
    updated_at: plan.updated_at || '',
    ids: kept.concat(keys.filter(function (k) { return kept.indexOf(k) === -1; })),
  };
  try {
    localStorage.setItem(ANSWERED_KEY, JSON.stringify(marks));
  } catch (_) { /* memory still holds them for this page */ }
}

// ------------------------------------------------------------ compose

function oneLine(text) {
  return String(text || '').split(/\s+/).filter(Boolean).join(' ');
}

function answerText(item, draft) {
  const picked = (item.options || [])
    .filter(function (_opt, i) { return draft.picks.indexOf(i) !== -1; })
    .map(function (opt) { return opt.label + (opt.recommended ? ' (recommended)' : ''); });
  const parts = [];
  if (picked.length) parts.push(picked.join(', '));
  const other = oneLine(draft.other);
  if (other) parts.push('Other: ' + other);
  return parts.join('; ');
}

// The one message Done sends. One line per answered item, numbered by its
// position in the plan, tagged with its ref (or repo) and — when the chief
// gave it one — its id, so the chief can find the item to remove:
//   Answers from the Board (2 of 3):
//   1. [fleet-config#959] Approve the four plans? → Yes, all four (recommended) {id: q-plans}
//   2. [life-os#171] Blank the old passwords? → Other: only the transcripts
//   Skipped: 3
//   Also: <anything-else text>
export function composeAnswers(items, drafts, also) {
  const lines = [];
  const skipped = [];
  items.forEach(function (item, i) {
    const answer = answerText(item, drafts[i]);
    if (!answer) {
      skipped.push(i + 1);
      return;
    }
    const tag = item.ref || item.repo;
    lines.push((i + 1) + '. ' + (tag ? '[' + tag + '] ' : '')
      + oneLine(item.question || item.text) + ' → ' + answer
      + (item.id ? ' {id: ' + item.id + '}' : ''));
  });
  const out = ['Answers from the Board (' + lines.length + ' of ' + items.length + '):'].concat(lines);
  if (skipped.length) out.push('Skipped: ' + skipped.join(', '));
  const extra = oneLine(also);
  if (extra) out.push('Also: ' + extra);
  return out.join('\n');
}

function answeredCount() {
  return sheet.items.filter(function (item, i) {
    return !!answerText(item, sheet.drafts[i]);
  }).length;
}

// --------------------------------------------------------------- render

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function paintItem(block, draft) {
  block.querySelectorAll('.tr-ask-opt').forEach(function (b) {
    b.setAttribute('aria-pressed', draft.picks.indexOf(Number(b.dataset.i)) !== -1 ? 'true' : 'false');
  });
}

function renderItem(item, index, answered) {
  const draft = sheet.drafts[index];
  const block = el('section', 'tr-ask chief-answer');
  block.dataset.key = itemKey(item, index);

  const head = el('div', 'tr-ask-head chief-answer-head');
  head.appendChild(el('span', 'chief-answer-num', String(index + 1)));
  if (item.repo) head.appendChild(el('span', 'tr-ask-header chief-answer-repo', item.repo));
  if (item.ref) {
    if (item.ref_url) {
      const a = el('a', 'chief-answer-ref', item.ref);
      a.href = item.ref_url;
      a.target = '_blank';
      a.rel = 'noopener';
      head.appendChild(a);
    } else {
      head.appendChild(el('span', 'chief-answer-ref', item.ref));
    }
  }
  block.appendChild(head);

  const question = item.question || item.text;
  block.appendChild(el('p', 'tr-ask-question', question));
  if (item.detail) block.appendChild(el('p', 'tr-ask-hint chief-answer-detail', item.detail));
  if (item.recommendation) {
    const rec = el('p', 'chief-answer-rec');
    rec.innerHTML = icon('sparkle');
    rec.appendChild(el('span', 'chief-answer-rec-label', 'Recommended: '));
    rec.appendChild(document.createTextNode(item.recommendation));
    block.appendChild(rec);
  }

  const options = item.options || [];
  // Other, on every item; an item with no options is answered here alone.
  const input = el('input', 'tr-ask-input');
  input.type = 'text';
  input.maxLength = 500;
  input.placeholder = options.length ? 'Other' : 'Your answer';
  input.setAttribute('aria-label', (options.length ? 'Other answer to: ' : 'Answer to: ') + (question || 'the question'));
  input.value = draft.other;
  input.addEventListener('input', function () {
    draft.other = input.value;
    // Single-select: typing is the answer, so it drops a pick.
    if (!item.multi && input.value.trim() && draft.picks.length) {
      draft.picks = [];
      paintItem(block, draft);
    }
    syncDone();
  });

  if (options.length) {
    if (item.multi) block.appendChild(el('p', 'tr-ask-hint', 'Pick any number'));
    const list = el('div', 'tr-ask-options');
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', question || 'Options');
    options.forEach(function (opt, oi) {
      const b = el('button', 'tr-ask-opt');
      b.type = 'button';
      b.dataset.i = String(oi);
      if (opt.recommended) b.classList.add('is-recommended');
      const body = el('span', 'tr-ask-opt-body');
      body.appendChild(el('span', 'tr-ask-label', opt.label + (opt.recommended ? ' (Recommended)' : '')));
      if (opt.description) body.appendChild(el('span', 'tr-ask-desc', opt.description));
      const mark = el('span', 'tr-ask-mark');
      mark.setAttribute('aria-hidden', 'true');
      mark.innerHTML = icon('circle-check');
      b.append(el('span', 'tr-ask-num', String(oi + 1)), body, mark);
      b.addEventListener('click', function () {
        const at = draft.picks.indexOf(oi);
        if (item.multi) {
          if (at === -1) draft.picks.push(oi); else draft.picks.splice(at, 1);
        } else {
          // Single-select: a tap picks this one (or clears it), and replaces
          // anything typed into Other, as the transcript card does.
          draft.picks = at === -1 ? [oi] : [];
          if (draft.picks.length) {
            draft.other = '';
            input.value = '';
          }
        }
        paintItem(block, draft);
        syncDone();
      });
      list.appendChild(b);
    });
    block.appendChild(list);
  }

  const other = el('div', 'tr-ask-other');
  other.appendChild(input);
  block.appendChild(other);

  if (answered) {
    block.classList.add('is-answered');
    block.appendChild(el('p', 'tr-ask-status chief-answer-sent', 'Answered, waiting for the chief'));
  }
  paintItem(block, draft);
  return block;
}

// Done is live only with the chief running and something to say; the note
// says why when it isn't, never a silent grey button.
function syncDone() {
  const done = els.chiefAnswersDone;
  const note = els.chiefAnswersNote;
  if (!done || !sheet) return;
  let why = '';
  if (chiefRun !== 'running') {
    why = chiefRun === 'unknown' ? 'Chief status unknown — session-host unreachable.' : 'Chief not running.';
  } else if (!answeredCount() && !els.chiefAnswersAlso.value.trim()) {
    why = 'Pick or type an answer first. Unanswered questions are sent as skipped.';
  }
  done.disabled = sending || !!why;
  note.textContent = why;
  note.hidden = !why;
}

// ---------------------------------------------------------------- public

// The Board's poll reports whether the chief runs ('running' | 'stopped' |
// 'unknown'); an open sheet re-gates Done on it.
export function syncChiefAnswers(run) {
  chiefRun = run || '';
  syncDone();
}

export function openChiefAnswers(plan, run) {
  const dialog = els.chiefAnswersDialog;
  if (!dialog || !plan) return;
  const items = plan.waiting_on_roberto || [];
  if (!items.length) return;
  // Reopening the same version of the plan keeps what was typed.
  const same = sheet && sheet.plan.updated_at === plan.updated_at
    && sheet.items.length === items.length
    && sheet.items.every(function (it, i) { return itemKey(it, i) === itemKey(items[i], i); });
  sheet = {
    plan: plan,
    items: items,
    drafts: same ? sheet.drafts : items.map(function () { return { picks: [], other: '' }; }),
  };
  if (!same) els.chiefAnswersAlso.value = '';
  const answered = answeredKeys(plan);
  els.chiefAnswersList.replaceChildren.apply(els.chiefAnswersList, items.map(function (item, i) {
    return renderItem(item, i, answered.indexOf(itemKey(item, i)) !== -1);
  }));
  syncChiefAnswers(run);
  if (!dialog.open) dialog.showModal();
}

async function submitAnswers() {
  if (!sheet || sending) return;
  const text = composeAnswers(sheet.items, sheet.drafts, els.chiefAnswersAlso.value);
  const done = els.chiefAnswersDone;
  sending = true;
  syncDone();
  const stopTimer = startWorkTimer(done, DONE_LABEL);
  try {
    await sendToChief(text);
    markAnswered(sheet.plan, sheet.items
      .map(function (item, i) { return answerText(item, sheet.drafts[i]) ? itemKey(item, i) : null; })
      .filter(function (k) { return k !== null; }));
    // Sent: the drafts are done with, so the next open starts clean.
    sheet = null;
    els.chiefAnswersAlso.value = '';
    els.chiefAnswersDialog.close();
    toast('Sent to chief', 'good', { icon: 'crown' });
    renderBoard();
  } catch (exc) {
    // The sheet stays open with every answer in it, to send again.
    apiFailToast('Answers not sent', exc);
  } finally {
    stopTimer();
    sending = false;
    syncDone();
  }
}

export function wireChiefAnswers() {
  if (!els.chiefAnswersDialog) return;
  els.chiefAnswersClose.addEventListener('click', function () {
    els.chiefAnswersDialog.close();
  });
  els.chiefAnswersAlso.addEventListener('input', syncDone);
  els.chiefAnswersDone.addEventListener('click', submitAnswers);
}
