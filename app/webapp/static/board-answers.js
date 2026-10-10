/* The chief's answer sheet (issue #1295).
 *
 * The Chief's-plan card (#1279, board.js) lists what waits on Roberto. This
 * module is the one-shot way to answer all of it: a sheet in the style of the
 * transcript's AskUserQuestion card (#1149, the same `.tr-ask-*` rows) with
 * one block per waiting item, in file order: the repo and ref, the question,
 * the detail, the options (single- or multi-select, the recommended one
 * labelled "(Recommended)"), an Other field on every item (with a mic when
 * dictation is available, #1450), and an "Anything else"
 * box under them all. The chief's recommendation shows as its own
 * quiet line only when it marks no option — the label already says it.
 * Anything else is the shared composer (composer.js, as a field): dictation
 * and image paste as in Chat, an attachment stored on the chief's session
 * and appended as its path, exactly as a Chat send carries it. Done composes
 * one plain-text message and sends it through the path the chat bar already
 * uses (board-dispatch.js::sendToChief), so the sheet adds no second way into
 * the chief's PTY.
 *
 * The app never writes the plan file: the chief removes the items it has
 * taken. Until the plan's `updated_at` moves, the items sent are marked
 * "answered, waiting for the chief" on every device (#1487): Done records
 * them with the webapp (POST /api/board/chief/answered), and the plan the
 * Board polls carries them back as `answered`. This page also remembers what
 * it sent, in memory, so its own marks show at once and survive a record
 * that failed. The card's "Answer N questions" and the sheet skip answered
 * items.
 *
 * The sheet is built when it opens and never by the 5 s poll, so a poll
 * can't wipe an answer being typed; the poll only re-gates Done
 * (syncChiefAnswers) on whether the chief is running.
 */

import { els, state } from './state.js';
import { apiFailToast, toast } from './api.js';
import { terminalJsonApi } from './webauthn.js';
import { askOption } from './ask-option.js';
import { icon } from './_vendored/icons/icons.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from './voice.js';
import { growTextarea, mountComposer } from './composer.js';
import { uploadSessionFile } from './terminal-compose.js';
import { chiefSessionId, sendToChief } from './board-dispatch.js';
import { renderBoard } from './board.js';

const DONE_LABEL = 'Done';
// Question and answer are joined by an arrow in the message the chief
// parses. It is message text, never a rendered glyph, so it is spelled as an
// escape (the #1127 no-glyph guard reads raw source).
const ANSWER_SEP = ' \u2192 ';

// The open sheet: the plan it was built from, one draft per item
// ({ picks: [option index], other: '' }), and Anything else's text while the
// sheet is closed (the composer drops its own on close).
let sheet = null;
// Anything else: the shared composer, mounted once.
let also = null;
// One dictation per Other field in the open sheet (#1450), disposed when the
// sheet closes or is rebuilt so no mic outlives the fields it types into.
let otherDictations = [];
// Whether the chief is running, as the Board last read it.
let chiefRun = '';
let sending = false;

// ------------------------------------------------------ answered marks

// What this page sent, on which version of the plan.
let sent = { updated_at: null, ids: [] };

function union(a, b) {
  return a.concat(b.filter(function (k) { return a.indexOf(k) === -1; }));
}

// The item's key: the chief's own id, else its position in the list.
export function itemKey(item, index) {
  return item.id || String(index);
}

// The keys answered on this version of the plan: from any device, as the
// webapp recorded them, plus what this page sent.
export function answeredKeys(plan) {
  if (!plan) return [];
  const recorded = Array.isArray(plan.answered) ? plan.answered.map(String) : [];
  return sent.updated_at === (plan.updated_at || '') ? union(recorded, sent.ids) : recorded;
}

function markSent(plan, keys) {
  const version = plan.updated_at || '';
  sent = { updated_at: version, ids: union(sent.updated_at === version ? sent.ids : [], keys) };
}

// Share the marks with every device. The webapp records them only while
// this is still the plan on disk; whatever it answers is the current marks,
// so the card shows them without waiting for the next poll.
async function recordAnswered(plan, keys) {
  try {
    const res = await terminalJsonApi('/api/board/chief/answered', {
      method: 'POST',
      body: { updated_at: plan.updated_at || '', ids: keys },
    });
    const now = state.chiefPlan;
    if (now && now.state === 'ok' && (now.updated_at || '') === res.updated_at) {
      now.answered = res.answered;
      renderBoard();
    }
  } catch (exc) {
    apiFailToast('Sent, but other devices still show these questions open', exc);
  }
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
// Anything else goes as typed (trimmed), not folded to one line: an attached
// file is its own paragraph, as a Chat send carries it. `shown` lists the
// plan positions the sheet offered (a Board repo filter narrows it, #1332);
// an item the filter hid was never asked, so it is neither answered nor
// "Skipped", and the count is out of what was shown.
export function composeAnswers(items, drafts, extra, shown) {
  const lines = [];
  const skipped = [];
  const indices = shown || allIndices(items);
  indices.forEach(function (i) {
    const item = items[i];
    const answer = answerText(item, drafts[i]);
    if (!answer) {
      skipped.push(i + 1);
      return;
    }
    const tag = item.ref || item.repo;
    lines.push((i + 1) + '. ' + (tag ? '[' + tag + '] ' : '')
      + oneLine(item.question || item.text) + ANSWER_SEP + answer
      + (item.id ? ' {id: ' + item.id + '}' : ''));
  });
  const out = ['Answers from the Board (' + lines.length + ' of ' + indices.length + '):'].concat(lines);
  if (skipped.length) out.push('Skipped: ' + skipped.join(', '));
  const also = String(extra || '').trim();
  if (also) out.push('Also: ' + also);
  return out.join('\n');
}

function alsoText() {
  return also ? also.textarea.value : '';
}

function allIndices(items) {
  return items.map(function (_item, i) { return i; });
}

function answeredCount() {
  return sheet.shown.filter(function (i) {
    return !!answerText(sheet.items[i], sheet.drafts[i]);
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

function renderItem(item, index) {
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
  const options = item.options || [];
  // A recommended option says so in its own label; only a recommendation
  // that marks none needs a line of its own — quiet text, not a card.
  if (item.recommendation && !options.some(function (o) { return o.recommended; })) {
    block.appendChild(el('p', 'tr-ask-hint chief-answer-rec', 'Recommended: ' + item.recommendation));
  }

  // Other, on every item; an item with no options is answered here alone.
  const input = el('input', 'tr-ask-input');
  input.type = 'text';
  input.maxLength = 500;
  input.placeholder = options.length ? 'Other' : 'Your answer';
  input.setAttribute('aria-label', (options.length ? 'Other answer to: ' : 'Answer to: ') + (question || 'the question'));
  input.value = draft.other;
  // Typed or dictated (#1450): the field's text is the draft.
  function otherChanged() {
    draft.other = input.value;
    // Single-select: typing is the answer, so it drops a pick.
    if (!item.multi && input.value.trim() && draft.picks.length) {
      draft.picks = [];
      paintItem(block, draft);
    }
    syncDone();
  }
  input.addEventListener('input', otherChanged);

  if (options.length) {
    if (item.multi) block.appendChild(el('p', 'tr-ask-hint', 'Pick any number'));
    const list = el('div', 'tr-ask-options');
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', question || 'Options');
    options.forEach(function (opt, oi) {
      const b = askOption({
        n: oi + 1,
        label: opt.label + (opt.recommended ? ' (Recommended)' : ''),
        description: opt.description,
        mark: true,
      });
      b.dataset.i = String(oi);
      if (opt.recommended) b.classList.add('is-recommended');
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
  // The same dictation Anything else and the chat bar use, bound to this
  // field; the transcript lands in the field for review before Done.
  if (voiceDictationAvailable()) {
    const mic = el('button', 'icon-button compose-record tr-ask-mic');
    mic.type = 'button';
    mic.title = 'Dictate (voice to text)';
    mic.setAttribute('aria-label', 'Dictate');
    mic.setAttribute('aria-pressed', 'false');
    mic.innerHTML = icon('mic');
    const dictation = createDictation({
      button: mic,
      getTextarea: function () { return input; },
      onRender: otherChanged,
    });
    mic.addEventListener('click', dictation.toggle);
    otherDictations.push(dictation);
    other.appendChild(mic);
  }
  block.appendChild(other);
  paintItem(block, draft);
  return block;
}

function disposeOtherDictations() {
  otherDictations.forEach(function (d) { d.dispose(); });
  otherDictations = [];
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
  } else if (!answeredCount() && !alsoText().trim()) {
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

// `shown` is the plan positions to ask: the Board's repo filter (#1332)
// minus what is already answered (#1487); omitted, every item. Drafts stay
// indexed by plan position, so narrowing or widening it between opens keeps
// whatever was typed.
export function openChiefAnswers(plan, run, shown) {
  const dialog = els.chiefAnswersDialog;
  if (!dialog || !plan) return;
  const items = plan.waiting_on_roberto || [];
  const indices = shown || allIndices(items);
  if (!indices.length) return;
  // Reopening the same version of the plan keeps what was typed.
  const same = sheet && sheet.plan.updated_at === plan.updated_at
    && sheet.items.length === items.length
    && sheet.items.every(function (it, i) { return itemKey(it, i) === itemKey(items[i], i); });
  sheet = {
    plan: plan,
    items: items,
    shown: indices,
    drafts: same ? sheet.drafts : items.map(function () { return { picks: [], other: '' }; }),
    also: same ? sheet.also : '',
  };
  also.textarea.value = sheet.also;
  growTextarea(also.textarea);
  also.setAvailability({
    dictate: voiceDictationAvailable(),
    ocr: !!(state.status && state.status.screenshot_ocr),
  });
  disposeOtherDictations();
  els.chiefAnswersList.replaceChildren.apply(els.chiefAnswersList, indices.map(function (i) {
    return renderItem(items[i], i);
  }));
  syncChiefAnswers(run);
  if (!dialog.open) dialog.showModal();
}

async function submitAnswers() {
  if (!sheet || sending) return;
  // A stopped dictation is still settling into the box (#489): wait for it,
  // as the composer's own Send does.
  if (also.isBusy() || otherDictations.some(function (d) { return d.isBusy(); })) {
    toast('Still transcribing — wait for the transcript, then tap Done', 'error', { icon: 'mic' });
    return;
  }
  const text = composeAnswers(sheet.items, sheet.drafts, alsoText(), sheet.shown);
  const done = els.chiefAnswersDone;
  sending = true;
  syncDone();
  const stopTimer = startWorkTimer(done, DONE_LABEL);
  try {
    await sendToChief(text);
    const keys = sheet.shown
      .filter(function (i) { return !!answerText(sheet.items[i], sheet.drafts[i]); })
      .map(function (i) { return itemKey(sheet.items[i], i); });
    markSent(sheet.plan, keys);
    // Sent: the drafts it carried are done with, so the next open starts
    // clean. One typed for an item the filter hid was not sent, and stays.
    // The close handler saves the box into `sheet.also`, so empty the box.
    sheet.shown.forEach(function (i) { sheet.drafts[i] = { picks: [], other: '' }; });
    also.textarea.value = '';
    sheet.also = '';
    els.chiefAnswersDialog.close();
    toast('Sent to chief', 'good', { icon: 'crown' });
    renderBoard();
    if (keys.length) recordAnswered(sheet.plan, keys);
  } catch (exc) {
    // The sheet stays open with every answer in it, to send again.
    apiFailToast('Answers not sent', exc);
  } finally {
    stopTimer();
    sending = false;
    syncDone();
  }
}

// An attachment is stored on the running chief's session, as Chat stores one
// on its session's; with no chief there is nowhere to put it.
function uploadToChief(file, signal, kind) {
  const sid = chiefSessionId();
  if (!sid) return Promise.reject(new Error('the chief is not running'));
  return uploadSessionFile(sid, file, signal, kind);
}

export function wireChiefAnswers() {
  if (!els.chiefAnswersDialog) return;
  also = mountComposer(els.chiefAnswersAlso, {
    placeholder: 'Anything else for the chief',
    field: true,
    upload: uploadToChief,
    onChange: syncDone,
  });
  also.setPlaceholder('Anything else for the chief');
  els.chiefAnswersClose.addEventListener('click', function () {
    els.chiefAnswersDialog.close();
  });
  // However it closes (the X, Esc, a send): keep the text for a reopen of
  // the same plan, then reset the composer so a recording or a staged
  // screenshot never outlives the sheet (#755).
  els.chiefAnswersDialog.addEventListener('close', function () {
    if (sheet) sheet.also = alsoText();
    also.reset();
    disposeOtherDictations();
  });
  els.chiefAnswersDone.addEventListener('click', submitAnswers);
}
