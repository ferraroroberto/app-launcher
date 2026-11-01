/* The chief's answer sheet.
 *
 * The Chief's-plan card (board.js) lists what waits on Roberto. This
 * module is the one-shot way to answer all of it: a sheet in the style of the
 * transcript's AskUserQuestion card (the same `.tr-ask-*` rows) with
 * one block per waiting item, in file order: the repo and ref, the question,
 * the detail, the options (single- or multi-select, the recommended one
 * labelled "(Recommended)"), an Other field on every item (with a mic when
 * dictation is available), and an "Anything else"
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
 * "answered, waiting for the chief" on every device: Done records
 * them with the webapp (POST /api/board/chief/answered), and the plan the
 * Board polls carries them back as `answered`. This page also remembers what
 * it sent, in memory, so its own marks show at once and survive a record
 * that failed. The card's "Answer N questions" and the sheet skip answered
 * items.
 *
 * The sheet is built when it opens and never by the 5 s poll, so a poll
 * can't wipe an answer being typed; the poll only re-gates Done
 * (syncChiefAnswers) on whether the chief is running.
 *
 * Owner checks (owner-checks.js) are answered in the same sheet, after the
 * chief's questions. Done writes their answers to GitHub first, each on its
 * own, then tells the chief what landed in the same one message. Validated,
 * Done and Later need no chief; a Problem does, since the chief files the fix.
 */

import { els, state } from '../state.js';
import { apiFailToast, toast } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { askOption } from '../chat/ask-option.js';
import { icon } from '../_vendored/icons/icons.js';
import { el } from '../dom-utils.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from '../voice.js';
import { growTextarea, mountComposer } from '../composer.js';
import { uploadSessionFile } from '../terminal/terminal-compose.js';
import { chiefSessionId, sendToChief } from './board-dispatch.js';
import { renderBoard } from '../board.js';
import { itemKey, markSent } from './chief-keys.js';
import {
  emptyOwnerDraft, ownerAnswerOf, ownerMessageLine, ownerSheetBlock, ownerSheetKey,
  postOwnerAnswers, shownOwnerItems,
} from './owner-checks.js';

const DONE_LABEL = 'Done';
// Question and answer are joined by an arrow in the message the chief
// parses. It is message text, never a rendered glyph, so it is spelled as an
// escape (the no-glyph guard reads raw source).
const ANSWER_SEP = ' \u2192 ';

// The open sheet: the plan it was built from, one draft per item
// ({ picks: [option index], other: '' }), and Anything else's text while the
// sheet is closed (the composer drops its own on close).
let sheet = null;
// Anything else: the shared composer, mounted once.
let also = null;
// One dictation per Other field in the open sheet, disposed when the
// sheet closes or is rebuilt so no mic outlives the fields it types into.
let otherDictations = [];
// Whether the chief is running, as the Board last read it.
let chiefRun = '';
let sending = false;
// Owner-check drafts by ref, kept across opens until their answer lands.
const ownerDrafts = {};

function ownerDraft(item) {
  if (!ownerDrafts[item.ref]) ownerDrafts[item.ref] = emptyOwnerDraft();
  return ownerDrafts[item.ref];
}

// ------------------------------------------------------ answered marks

// The keys, and this page's own marks, live in chief-keys.js, shared
// with the Board's counts.

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
//   1. [fleet-config#<n>] Approve the four plans? → Yes, all four (recommended) {id: q-plans}
//   2. [life-os#<n>] Blank the old passwords? → Other: only the transcripts
//   Skipped: 3
//   Also: <anything-else text>
// Anything else goes as typed (trimmed), not folded to one line: an attached
// file is its own paragraph, as a Chat send carries it. `shown` lists the
// plan positions the sheet offered (a Board repo filter narrows it);
// an item the filter hid was never asked, so it is neither answered nor
// "Skipped", and the count is out of what was shown.
export function composeAnswers(items, drafts, extra, shown, ownerLines) {
  const lines = [];
  const skipped = [];
  const indices = shown || allIndices(items);
  const owner = ownerLines || [];
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
  const out = indices.length || !owner.length
    ? ['Answers from the Board (' + lines.length + ' of ' + indices.length + '):'].concat(lines)
    : [];
  if (skipped.length) out.push('Skipped: ' + skipped.join(', '));
  if (owner.length) out.push('Owner checks answered on the Board:');
  owner.forEach(function (line) { out.push(line); });
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

function ownerAnswers() {
  return sheet.owner
    .map(function (item) { return { item: item, ans: ownerAnswerOf(item, ownerDraft(item)) }; })
    .filter(function (x) { return !!x.ans; });
}

// --------------------------------------------------------------- render

function paintItem(block, draft) {
  block.querySelectorAll('.tr-ask-opt').forEach(function (b) {
    b.setAttribute('aria-pressed', draft.picks.indexOf(Number(b.dataset.i)) !== -1 ? 'true' : 'false');
  });
}

function renderItem(item, index) {
  const draft = sheet.drafts[index];
  const block = el('section', 'tr-ask chief-answer');
  block.dataset.key = itemKey(item, index);

  block.appendChild(answerHead(item, index));

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
  // Typed or dictated: the field's text is the draft.
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
  if (voiceDictationAvailable()) other.appendChild(otherMic(input, otherChanged));
  block.appendChild(other);
  paintItem(block, draft);
  return block;
}

// An item's head: its number, then its repo and its ref (a link when it has one).
function answerHead(item, index) {
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
  return head;
}

// The Other field's dictation button; `onRender` runs on each transcript.
function otherMic(input, onRender) {
  const mic = el('button', 'icon-button compose-record tr-ask-mic');
  mic.type = 'button';
  mic.title = 'Dictate (voice to text)';
  mic.setAttribute('aria-label', 'Dictate');
  mic.setAttribute('aria-pressed', 'false');
  mic.innerHTML = icon('mic');
  const dictation = createDictation({
    button: mic,
    getTextarea: function () { return input; },
    onRender: onRender,
  });
  mic.addEventListener('click', dictation.toggle);
  otherDictations.push(dictation);
  return mic;
}

function disposeOtherDictations() {
  otherDictations.forEach(function (d) { d.dispose(); });
  otherDictations = [];
}

// Done is live with something to say and, when any of it is for the chief
// (its questions, Anything else, a Problem), the chief running; the note
// says why when it isn't, never a silent grey button.
function syncDone() {
  const done = els.chiefAnswersDone;
  const note = els.chiefAnswersNote;
  if (!done || !sheet) return;
  const owner = ownerAnswers();
  const forChief = answeredCount() > 0 || !!alsoText().trim()
    || owner.some(function (x) { return x.ans.answer === 'problem'; });
  let why = '';
  if (!forChief && !owner.length) {
    why = 'Pick or type an answer first. Unanswered questions are sent as skipped.';
  } else if (forChief && chiefRun !== 'running') {
    why = chiefRun === 'unknown' ? 'Chief status unknown — session-host unreachable.' : 'Chief not running.';
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

// `shown` is the plan positions to ask: the Board's repo filter
// minus what is already answered; omitted, every item. Drafts stay
// indexed by plan position, so narrowing or widening it between opens keeps
// whatever was typed. `startAt`, a plan position among them, opens
// the sheet scrolled to that question: a tap on its Board row.
// `startOwner`, an owner check's ref, opens it scrolled to that owner check
// instead. Owner checks the Board shows (owner-checks.js) follow the
// chief's questions; with no plan to answer, the sheet asks those alone.
export function openChiefAnswers(plan, run, shown, startAt, startOwner) {
  const dialog = els.chiefAnswersDialog;
  if (!dialog) return;
  const ok = !!plan && plan.state === 'ok';
  const items = ok ? plan.waiting_on_roberto || [] : [];
  const indices = ok ? shown || allIndices(items) : [];
  const owner = shownOwnerItems(state.board);
  if (!indices.length && !owner.length) return;
  const version = ok ? plan.updated_at || '' : null;
  // Reopening the same version of the plan keeps what was typed.
  const same = sheet && sheet.version === version
    && sheet.items.length === items.length
    && sheet.items.every(function (it, i) { return itemKey(it, i) === itemKey(items[i], i); });
  sheet = {
    plan: ok ? plan : null,
    version: version,
    items: items,
    owner: owner,
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
  const blocks = indices.map(function (i) { return renderItem(items[i], i); });
  owner.forEach(function (item) {
    blocks.push(ownerSheetBlock(item, ownerDraft(item), syncDone, function (wrap, input, typed) {
      if (voiceDictationAvailable()) wrap.appendChild(otherMic(input, typed));
    }));
  });
  els.chiefAnswersList.replaceChildren.apply(els.chiefAnswersList, blocks);
  els.chiefAnswersTitleText.textContent = indices.length ? 'Answer the chief' : 'Owner checks';
  syncChiefAnswers(run);
  if (!dialog.open) dialog.showModal();
  let key = null;
  if (startOwner) key = ownerSheetKey({ ref: startOwner });
  else if (startAt != null) key = itemKey(items[startAt], startAt);
  const start = key == null ? null
    : els.chiefAnswersList.querySelector('[data-key="' + CSS.escape(key) + '"]');
  // The dialog is the scroller, scrolled by hand two frames on: in WebKit
  // the modal's autofocused title scrolls itself into view on the
  // first frame after showModal(), undoing an earlier scroll, and it ignores
  // scrollIntoView() inside the modal altogether.
  if (start) {
    requestAnimationFrame(function () { requestAnimationFrame(function () {
      dialog.scrollTop += start.getBoundingClientRect().top - dialog.getBoundingClientRect().top;
    }); });
  }
}

// The owner-check answers go to GitHub first, each applied on its own.
// One that landed drops its draft and becomes a line for the chief; one that
// failed keeps its draft and is counted for the toast.
async function applyOwnerAnswers(answered) {
  if (!answered.length) return { lines: [], failed: 0 };
  const res = await postOwnerAnswers(answered.map(function (x) { return x.ans; }));
  const results = (res && res.results) || [];
  const lines = [];
  let failed = 0;
  answered.forEach(function (x) {
    const r = results.find(function (y) { return y.ref === x.ans.ref; });
    if (r && r.ok) {
      delete ownerDrafts[x.item.ref];
      const line = ownerMessageLine(x.item, x.ans);
      if (line) lines.push(line);
    } else {
      failed += 1;
    }
  });
  if (res && res.owner_checks && state.board) state.board.owner_checks = res.owner_checks;
  return { lines: lines, failed: failed };
}

async function submitAnswers() {
  if (!sheet || sending) return;
  // A stopped dictation is still settling into the box: wait for it,
  // as the composer's own Send does.
  if (also.isBusy() || otherDictations.some(function (d) { return d.isBusy(); })) {
    toast('Still transcribing — wait for the transcript, then tap Done', 'error', { icon: 'mic' });
    return;
  }
  const done = els.chiefAnswersDone;
  // A failed owner check rebuilds the sheet below; keep this one's plan.
  const plan = sheet.plan;
  sending = true;
  syncDone();
  const stopTimer = startWorkTimer(done, DONE_LABEL);
  let owner = { lines: [], failed: 0 };
  try {
    owner = await applyOwnerAnswers(ownerAnswers());
    const keys = sheet.shown
      .filter(function (i) { return !!answerText(sheet.items[i], sheet.drafts[i]); })
      .map(function (i) { return itemKey(sheet.items[i], i); });
    const forChief = keys.length > 0 || !!alsoText().trim() || owner.lines.length > 0;
    // With no chief running only Validated / Done / Later get this far
    // (syncDone): GitHub holds them, and the chief reads them there.
    const tell = forChief && chiefRun === 'running';
    if (tell) {
      await sendToChief(composeAnswers(sheet.items, sheet.drafts, alsoText(), sheet.shown, owner.lines));
      if (plan) markSent(plan, keys);
      // Sent: the drafts it carried are done with, so the next open starts
      // clean. One typed for an item the filter hid was not sent, and stays.
      // The close handler saves the box into `sheet.also`, so empty the box.
      sheet.shown.forEach(function (i) { sheet.drafts[i] = { picks: [], other: '' }; });
      also.textarea.value = '';
      sheet.also = '';
    }
    renderBoard();
    if (owner.failed) {
      // The failed ones keep their drafts; the rebuilt sheet shows them.
      toast(owner.failed + (owner.failed === 1 ? ' owner check was' : ' owner checks were')
        + ' not saved on GitHub. Try again.', 'error');
      openChiefAnswers(plan, chiefRun, tell ? [] : sheet.shown);
    } else {
      els.chiefAnswersDialog.close();
      toast(tell ? 'Sent to chief' : 'Saved on GitHub', 'good', { icon: tell ? 'crown' : 'circle-check' });
    }
    if (tell && plan && keys.length) recordAnswered(plan, keys);
  } catch (exc) {
    // The sheet stays open with every answer not yet applied, to send again.
    apiFailToast(owner.lines.length ? 'Owner checks saved, but the chief was not told' : 'Answers not sent', exc);
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
  // screenshot never outlives the sheet.
  els.chiefAnswersDialog.addEventListener('close', function () {
    if (sheet) sheet.also = alsoText();
    also.reset();
    disposeOtherDictations();
  });
  els.chiefAnswersDone.addEventListener('click', submitAnswers);
}
