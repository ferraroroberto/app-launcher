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
 * Every item, in both sheets, also takes files (answer-files.js): a paperclip
 * and image paste beside its text, stored on the chief's session as Chat stores
 * them. A chief answer carries each path under its line; an owner-check answer
 * posts to GitHub only how many files it has, and the chief gets the paths in
 * the one message below.
 *
 * Owner checks (owner-checks.js) have their own sheet, the same dialog in
 * `owner` mode, its items drawn from the same option and Other parts as a chief
 * question (answer-item.js) and Anything else under them: the chief's
 * questions unblock work in flight, an owner check waits on the owner's own
 * schedule, so neither sheet lists the other's items. Done applies their
 * answers first, each on its own (GitHub, or the launcher's own mark), then
 * tells the chief what landed in one message. Validated, Done and Pending need
 * no chief; a Problem, a Pause or a Close does, since the chief acts on it.
 */

import { els, state } from '../state.js';
import { apiFailToast, toast } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { icon } from '../_vendored/icons/icons.js';
import { el } from '../dom-utils.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from '../voice.js';
import { growTextarea, mountComposer } from '../composer.js';
import { uploadSessionFile } from '../terminal/terminal-compose.js';
import { chiefSessionId, sendToChief } from './board-dispatch.js';
import { optionList, otherRow, paintOptions } from './answer-item.js';
import {
  attachedLines, disposeAnswerFiles, emptyFiles, filesUploading, mountAnswerFiles, syncAnswerFiles,
} from './answer-files.js';
import { renderBoard } from '../board.js';
import { itemKey, markSent } from './chief-keys.js';
import {
  emptyOwnerDraft, groupLabel, ownerAnswerOf, ownerMessageLine, ownerNeedsChief, ownerSheetBlock,
  ownerSheetKey, postOwnerAnswers, shownOwnerItems,
} from './owner-checks.js';

const DONE_LABEL = 'Done';
// Question and answer are joined by an arrow in the message the chief
// parses. It is message text, never a rendered glyph, so it is spelled as an
// escape (the no-glyph guard reads raw source).
const ANSWER_SEP = ' \u2192 ';

// The open sheet: its mode ('chief' | 'owner'), the plan it was built from,
// one draft per item ({ picks: [option index], other: '' }), and Anything
// else's text while the sheet is closed (the composer drops its own on close).
// The chief drafts stay in the sheet across an owner-mode open, so switching
// between the two sheets never loses what was typed.
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

function emptyChiefDraft() {
  return { picks: [], other: '', files: emptyFiles() };
}

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
//      Attached: <path>   (one per file, under its item's line)
//   2. [life-os#<n>] Blank the old passwords? → Other: only the transcripts
//   Skipped: 3
//   Also: <anything-else text>
// Anything else goes as typed (trimmed), not folded to one line: an attached
// file is its own paragraph, as a Chat send carries it. `shown` lists the
// plan positions the sheet offered (a Board repo filter narrows it);
// an item the filter hid was never asked, so it is neither answered nor
// "Skipped", and the count is out of what was shown.
export function composeAnswers(items, drafts, extra, shown, ownerLines, ownerGroup) {
  const lines = [];
  const skipped = [];
  let answered = 0;
  const indices = shown || allIndices(items);
  const owner = ownerLines || [];
  indices.forEach(function (i) {
    const item = items[i];
    const answer = answerText(item, drafts[i]);
    const files = drafts[i].files || [];
    if (!answer && !files.length) {
      skipped.push(i + 1);
      return;
    }
    answered++;
    const tag = item.ref || item.repo;
    lines.push((i + 1) + '. ' + (tag ? '[' + tag + '] ' : '')
      + oneLine(item.question || item.text) + ANSWER_SEP + (answer || '(files only)')
      + (item.id ? ' {id: ' + item.id + '}' : ''));
    attachedLines(files).forEach(function (line) { lines.push(line); });
  });
  const out = indices.length || !owner.length
    ? ['Answers from the Board (' + answered + ' of ' + indices.length + '):'].concat(lines)
    : [];
  if (skipped.length) out.push('Skipped: ' + skipped.join(', '));
  // The group the answers came from, after the colon, so the heading itself
  // stays the one the chief already reads.
  if (owner.length) out.push('Owner checks answered on the Board:' + (ownerGroup ? ' ' + ownerGroup : ''));
  owner.forEach(function (line) { out.push(line); });
  const also = String(extra || '').trim();
  if (also) out.push('Also: ' + also);
  return out.join('\n');
}

// Anything else, in either sheet: the owner's word to the chief beyond the items.
function alsoText() {
  return also && sheet ? also.textarea.value : '';
}

function allIndices(items) {
  return items.map(function (_item, i) { return i; });
}

// Files alone answer a chief question: they are what the owner has to say.
function hasAnswer(item, draft) {
  return !!answerText(item, draft) || draft.files.length > 0;
}

function answeredCount() {
  return sheet.shown.filter(function (i) { return hasAnswer(sheet.items[i], sheet.drafts[i]); }).length;
}

// An owner answer with the files that ride to the chief. Files with no answer
// are not here at all: they stay in their draft.
function ownerAnswers() {
  return sheet.owner
    .map(function (item) {
      const draft = ownerDraft(item);
      const ans = ownerAnswerOf(item, draft);
      return { item: item, ans: ans, files: ans ? draft.files.slice() : [] };
    })
    .filter(function (x) { return !!x.ans; });
}

// Files stored in the open sheet, both modes.
function sheetFileCount() {
  if (!sheet) return 0;
  return sheet.shown.reduce(function (n, i) { return n + sheet.drafts[i].files.length; }, 0)
    + sheet.owner.reduce(function (n, item) { return n + ownerDraft(item).files.length; }, 0);
}

// One item's files control: the paperclip, its progress and chips. The chief
// session takes the upload, so with none running it waits, with a note.
function itemFiles(draft, label) {
  return mountAnswerFiles({
    template: els.boardAnswerAttachTemplate,
    draft: draft,
    label: label,
    chiefOn: function () { return !!chiefSessionId(); },
    upload: uploadToChief,
    sheetUsed: sheetFileCount,
    onChange: syncDone,
  });
}

// --------------------------------------------------------------- render

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

  const paint = function () {
    paintOptions(block, function (i) { return draft.picks.indexOf(i) !== -1; });
  };
  // Other, on every item; an item with no options is answered here alone. The
  // field's text, typed or dictated, is the draft (the dictation is the one
  // Anything else and the chat bar use, its transcript landing in the field for
  // review before Done).
  const other = otherRow({
    value: draft.other,
    placeholder: options.length ? 'Other' : 'Your answer',
    ariaLabel: (options.length ? 'Other answer to: ' : 'Answer to: ') + (question || 'the question'),
    files: itemFiles(draft, 'question ' + (index + 1)),
    mic: voiceDictationAvailable() ? otherMic : null,
    onInput: function (field) {
      draft.other = field.value;
      // Single-select: typing is the answer, so it drops a pick.
      if (!item.multi && field.value.trim() && draft.picks.length) {
        draft.picks = [];
        paint();
      }
      syncDone();
    },
  });

  if (options.length) {
    if (item.multi) block.appendChild(el('p', 'tr-ask-hint', 'Pick any number'));
    block.appendChild(optionList({
      groupLabel: question || 'Options',
      options: options.map(function (opt) {
        return { label: opt.label + (opt.recommended ? ' (Recommended)' : ''), description: opt.description,
          recommended: opt.recommended };
      }),
      track: 'board.answer-option',
      onPick: function (oi) {
        const at = draft.picks.indexOf(oi);
        if (item.multi) {
          if (at === -1) draft.picks.push(oi); else draft.picks.splice(at, 1);
        } else {
          // Single-select: a tap picks this one (or clears it), and replaces
          // anything typed into Other, as the transcript card does.
          draft.picks = at === -1 ? [oi] : [];
          if (draft.picks.length) {
            draft.other = '';
            other.input.value = '';
          }
        }
        paint();
        syncDone();
      },
    }));
  }

  block.appendChild(other.row);
  block.appendChild(other.count);
  block.appendChild(other.files.extra);
  paint();
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
    || owner.some(function (x) { return ownerNeedsChief(x.ans, x.files); });
  let why = '';
  if (!forChief && !owner.length) {
    why = sheet.mode === 'owner'
      ? 'Pick an answer or type one in Other first.'
      : 'Pick or type an answer first. Unanswered questions are sent as skipped.';
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
  syncAnswerFiles();
  syncDone();
}

// The chief sheet. `shown` is the plan positions to ask: the Board's repo
// filter minus what is already answered; omitted, every item. Drafts stay
// indexed by plan position, so narrowing or widening it between opens keeps
// whatever was typed. `startAt`, a plan position among them, opens the sheet
// scrolled to that question: a tap on its Board row.
export function openChiefAnswers(plan, run, shown, startAt) {
  const ok = !!plan && plan.state === 'ok';
  const items = ok ? plan.waiting_on_roberto || [] : [];
  const indices = ok ? shown || allIndices(items) : [];
  if (!indices.length) return;
  openSheet('chief', plan, run, indices, [], startAt == null ? null : itemKey(items[startAt], startAt));
}

// The owner sheet: one group's owner checks the Board shows (`kind`, Before
// start or Validate; omitted, both), scrolled to `startRef` (the tapped item).
// It needs no plan; one that is there only keeps the chief sheet's drafts
// through the open.
export function openOwnerAnswers(plan, run, startRef, kind) {
  const owner = shownOwnerItems(state.board, kind);
  if (!owner.length) return;
  openSheet('owner', plan, run, [], owner, startRef ? ownerSheetKey({ ref: startRef }) : null, kind || null);
}

function openSheet(mode, plan, run, indices, owner, startKey, ownerKind) {
  const dialog = els.chiefAnswersDialog;
  if (!dialog) return;
  const ok = !!plan && plan.state === 'ok';
  const items = ok ? plan.waiting_on_roberto || [] : [];
  const version = ok ? plan.updated_at || '' : null;
  // Reopening the same version of the plan keeps what was typed.
  const same = sheet && sheet.version === version
    && sheet.items.length === items.length
    && sheet.items.every(function (it, i) { return itemKey(it, i) === itemKey(items[i], i); });
  sheet = {
    mode: mode,
    plan: ok ? plan : null,
    version: version,
    items: items,
    owner: owner,
    ownerKind: ownerKind || null,
    shown: indices,
    drafts: same ? sheet.drafts : items.map(emptyChiefDraft),
    also: same ? sheet.also : '',
  };
  const chief = mode === 'chief';
  also.textarea.value = sheet.also;
  growTextarea(also.textarea);
  also.setAvailability({
    dictate: voiceDictationAvailable(),
    ocr: !!(state.status && state.status.screenshot_ocr),
  });
  disposeOtherDictations();
  disposeAnswerFiles();
  const blocks = indices.map(function (i) { return renderItem(items[i], i); });
  owner.forEach(function (item) {
    const draft = ownerDraft(item);
    blocks.push(ownerSheetBlock(item, draft, syncDone, voiceDictationAvailable() ? otherMic : null,
      itemFiles(draft, item.ref)));
  });
  els.chiefAnswersList.replaceChildren.apply(els.chiefAnswersList, blocks);
  els.chiefAnswersTitleText.textContent = chief ? 'Answer the chief'
    : 'Owner checks' + (ownerKind ? ': ' + groupLabel(ownerKind) : '');
  syncChiefAnswers(run);
  if (!dialog.open) dialog.showModal();
  const start = startKey == null ? null
    : els.chiefAnswersList.querySelector('[data-key="' + CSS.escape(startKey) + '"]');
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
// failed keeps its draft, with the reason its block shows, and is listed for
// the toast. Never rejects: postOwnerAnswers reports a failed request per answer.
async function applyOwnerAnswers(answered) {
  if (!answered.length) return { lines: [], failed: [] };
  // Only the answer goes to GitHub, and in it only a file count (ownerAnswerOf).
  const res = await postOwnerAnswers(answered.map(function (x) { return x.ans; }));
  const lines = [];
  const failed = [];
  answered.forEach(function (x) {
    const r = res.results.find(function (y) { return y.ref === x.ans.ref; });
    if (r && r.ok) {
      delete ownerDrafts[x.item.ref];
      const line = ownerMessageLine(x.item, x.ans, x.files);
      if (line) lines.push(line);
    } else {
      ownerDraft(x.item).error = (r && r.error) || 'no result came back';
      failed.push(x.item.ref);
    }
  });
  if (res.owner_checks && state.board) state.board.owner_checks = res.owner_checks;
  return { lines: lines, failed: failed };
}

// The refs a toast names: the first three, then "and N more".
function refList(refs) {
  const shown = refs.slice(0, 3).join(', ');
  return refs.length > 3 ? shown + ' and ' + (refs.length - 3) + ' more' : shown;
}

async function submitAnswers() {
  if (!sheet || sending) return;
  // A stopped dictation is still settling into the box: wait for it,
  // as the composer's own Send does.
  if (also.isBusy() || otherDictations.some(function (d) { return d.isBusy(); })) {
    toast('Still transcribing — wait for the transcript, then tap Done', 'error', { icon: 'mic' });
    return;
  }
  if (filesUploading()) {
    toast('Still uploading — wait for the files, then tap Done', 'error', { icon: 'paperclip' });
    return;
  }
  const done = els.chiefAnswersDone;
  // A failed owner check rebuilds the sheet below; keep this one's plan.
  const plan = sheet.plan;
  sending = true;
  syncDone();
  const stopTimer = startWorkTimer(done, DONE_LABEL);
  let owner = { lines: [], failed: [] };
  try {
    owner = await applyOwnerAnswers(ownerAnswers());
    const keys = sheet.shown
      .filter(function (i) { return hasAnswer(sheet.items[i], sheet.drafts[i]); })
      .map(function (i) { return itemKey(sheet.items[i], i); });
    const forChief = keys.length > 0 || !!alsoText().trim() || owner.lines.length > 0;
    // With no chief running only Validated / Done / Later get this far
    // (syncDone): GitHub holds them, and the chief reads them there.
    const tell = forChief && chiefRun === 'running';
    if (tell) {
      await sendToChief(composeAnswers(sheet.items, sheet.drafts, alsoText(), sheet.shown, owner.lines,
        sheet.ownerKind ? groupLabel(sheet.ownerKind) : ''));
      if (plan) markSent(plan, keys);
      // Sent: the drafts it carried are done with, so the next open starts
      // clean. One typed for an item the filter hid was not sent, and stays.
      // The close handler saves the box into `sheet.also`, so empty the box.
      sheet.shown.forEach(function (i) { sheet.drafts[i] = emptyChiefDraft(); });
      also.textarea.value = '';
      sheet.also = '';
    }
    renderBoard();
    if (owner.failed.length) {
      // The failed ones keep their drafts; the rebuilt sheet shows each one's reason.
      toast('Not saved on GitHub: ' + refList(owner.failed) + '. Each says why; Done tries again.', 'error');
      openOwnerAnswers(plan, chiefRun, null, sheet.ownerKind);
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
export function uploadToChief(file, signal, kind) {
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
    disposeAnswerFiles();
  });
  els.chiefAnswersDone.addEventListener('click', submitAnswers);
}
