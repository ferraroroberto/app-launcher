/* Files on an answer-sheet item (the chief's questions and the owner checks).
 *
 * Each item gets the Chat composer's paperclip, image paste into its text
 * field, and a row of removable chips. The upload is Chat's own: the shared
 * attach queue (attach-batch.js) and the chief session's upload route, so there
 * is no second path and no new storage.
 *
 * The files live in the item's draft as a list of { name, path }, apart from
 * its text. That separation keeps a local path out of anything typed: an
 * owner-check answer posts only how many files it carries, never one of these
 * paths (owner_checks.py writes a fixed line from the count), and the chief
 * reads the paths from the one message the sheet sends its terminal.
 */

import { el } from '../dom-utils.js';
import { icon } from '../_vendored/icons/icons.js';
import { toast } from '../api.js';
import { cancelledSummary, createAttachQueue, summarize } from '../attach-batch.js';
import { trackFeature } from '../settings/feature-track.js';
import { pastedFiles } from '../composer.js';

export const MAX_ITEM_FILES = 5;
export const MAX_SHEET_FILES = 30;
export const NEEDS_CHIEF_NOTE = 'Start the chief to attach files.';

export function emptyFiles() {
  return [];
}

// "Attached: <path>" per file, each on its own line under the item's line.
export function attachedLines(files) {
  return (files || []).map(function (f) { return '   Attached: ' + f.path; });
}

// How many more files an item may take: the item's cap and the sheet's, each
// counting what is already stored plus what is still uploading.
export function roomFor(itemUsed, sheetUsed) {
  return Math.max(0, Math.min(MAX_ITEM_FILES - itemUsed, MAX_SHEET_FILES - sheetUsed));
}

export function tooManyText(room, offered) {
  return room <= 0
    ? 'No room for more files here (up to ' + MAX_ITEM_FILES + ' per question, ' + MAX_SHEET_FILES + ' in all).'
    : 'Only ' + room + ' of ' + offered + ' files fit (up to ' + MAX_ITEM_FILES + ' per question, '
      + MAX_SHEET_FILES + ' in all).';
}

// Every mounted item, so the sheet can count uploads in flight, wait for them
// and re-gate the controls when the chief starts or stops.
let live = [];

export function filesUploading() {
  return live.some(function (c) { return c.busy(); });
}

export function outstandingFiles() {
  return live.reduce(function (n, c) { return n + c.outstanding(); }, 0);
}

export function syncAnswerFiles() {
  live.forEach(function (c) { c.sync(); });
}

// Cancels what is uploading and forgets every control (the sheet closed or is
// rebuilt). Stored files stay in their drafts.
export function disposeAnswerFiles() {
  live.forEach(function (c) { c.cancel(); });
  live = [];
}

// One row per stored file: its name and a remove, which edits the draft.
function paintFileChips(chips, draft, onChange) {
  chips.replaceChildren.apply(chips, draft.files.map(function (f, i) {
    const li = el('li', 'tr-ask-file');
    li.appendChild(el('span', 'tr-ask-file-name', f.name));
    const rm = el('button', 'icon-button tr-ask-file-remove');
    rm.type = 'button';
    rm.setAttribute('aria-label', 'Remove ' + f.name);
    rm.title = 'Remove ' + f.name;
    rm.innerHTML = icon('x');
    rm.addEventListener('click', function () {
      draft.files.splice(i, 1);
      paintFileChips(chips, draft, onChange);
      onChange();
    });
    li.appendChild(rm);
    return li;
  }));
  chips.hidden = !draft.files.length;
}

// "N of M" on the paperclip and in the status line while a batch runs; both
// rest when it drains (null). The button stays live: a tap on it cancels.
function paintUploadProgress(button, status, p) {
  status.hidden = !p;
  status.textContent = p ? 'Uploading ' + p.index + ' of ' + p.total + ' · ' + p.name : '';
  button.classList.toggle('is-uploading', !!p);
  if (p) button.setAttribute('aria-busy', 'true');
  else button.removeAttribute('aria-busy');
  button.innerHTML = p
    ? icon('hourglass') + '<span class="tr-ask-attach-count">' + p.index + '/' + p.total + '</span>'
    : icon('paperclip');
}

// The one toast a run ends with; the queue's own wording says "added to the
// message", which an answer sheet's files are not.
function settleToast(r) {
  if (!r.total) return;
  if (!r.cancelled && !r.failures.length) {
    toast('Attached ' + r.ok + ' file' + (r.ok > 1 ? 's' : '') + '.', 'good', { icon: 'paperclip' });
    return;
  }
  const s = r.cancelled ? cancelledSummary(r.ok, r.total) : summarize(r.ok, r.total, r.failures);
  toast(s.text, s.tone, { icon: 'paperclip' });
}

// The item's nodes: the cloned paperclip, the hidden picker (no accept filter,
// so iOS offers Photos and Files), the progress line, the no-chief note and
// the chip list, the last four gathered in `extra`.
function buildFilesDom(template, label) {
  const button = template.content.firstElementChild.cloneNode(true);
  button.insertAdjacentHTML('afterbegin', icon('paperclip'));
  const picker = el('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.hidden = true;
  const status = el('p', 'tr-ask-hint tr-ask-upload', '');
  status.setAttribute('role', 'status');
  status.hidden = true;
  const note = el('p', 'tr-ask-hint tr-ask-files-note', NEEDS_CHIEF_NOTE);
  const chips = el('ul', 'tr-ask-files');
  chips.setAttribute('aria-label', 'Attached files for ' + label);
  const extra = el('div', 'tr-ask-files-wrap');
  [picker, status, note, chips].forEach(function (n) { extra.appendChild(n); });
  return { button: button, picker: picker, status: status, note: note, chips: chips, extra: extra };
}

// A tap on the paperclip opens the picker, or cancels a batch in flight.
function wireFilePicker(button, picker, queue, add) {
  button.addEventListener('click', function () {
    if (queue.busy()) queue.cancel();
    else picker.click();
  });
  picker.addEventListener('change', function () {
    const picked = Array.prototype.slice.call(picker.files || []);
    picker.value = '';
    add(picked);
  });
}

// Images pasted into the text field attach; a paste that carries text pastes it.
function wirePasteInto(input, add) {
  input.addEventListener('paste', function (ev) {
    const files = pastedFiles(ev.clipboardData);
    if (!files.length) return;
    ev.preventDefault();
    add(files);
  });
}

function logUpload(e) {
  console.info('[board-answers] upload', e.bytes + ' B', e.ms + ' ms', e.ok ? 'ok' : 'failed: ' + e.reason);
}

// opts:
//   template   the <template> holding the paperclip button
//   draft      the item's draft; its `files` is the list this edits
//   label      what the item is, for accessible names
//   chiefOn()  whether a chief session can take an upload
//   upload(file, signal)  stores one file, resolves its path
//   sheetUsed()  files stored in the sheet, all items
//   onChange() after the draft's files changed
// Returns { button, extra, wirePaste(input) }: the button goes in the item's
// field row, `extra` (progress line, note, chips) under it.
export function mountAnswerFiles(opts) {
  const draft = opts.draft;
  const { button, picker, status, note, chips, extra } = buildFilesDom(opts.template, opts.label);

  // Since the queue last went idle: files handed to it, and how many landed.
  let queued = 0;
  let landed = 0;

  const queue = createAttachQueue({
    upload: function (file, signal) { return opts.upload(file, signal); },
    onAppend: function (path, _kind, file) {
      landed++;
      draft.files.push({ name: (file && file.name) || 'file', path: path });
      trackFeature('feat.attach.board');
      paintChips();
      opts.onChange();
    },
    onProgress: function (p) {
      paintUploadProgress(button, status, p);
      label(!!p);
    },
    onSettle: function (r) {
      queued = 0;
      landed = 0;
      settleToast(r);
    },
    log: logUpload,
  });

  function label(uploading) {
    const text = uploading ? 'Cancel the upload' : 'Attach files to ' + opts.label;
    button.title = text;
    button.setAttribute('aria-label', text);
  }

  function used() { return draft.files.length + (queued - landed); }

  function paintChips() { paintFileChips(chips, draft, opts.onChange); }

  function add(files) {
    const list = Array.prototype.slice.call(files || []);
    if (!list.length || !opts.chiefOn()) return;
    const room = roomFor(used(), opts.sheetUsed() + outstandingFiles());
    const take = list.slice(0, room);
    if (take.length < list.length) toast(tooManyText(room, list.length), 'error', { icon: 'paperclip' });
    if (!take.length) return;
    queued += take.length;
    queue.enqueue(take);
  }

  const control = {
    busy: function () { return queue.busy(); },
    outstanding: function () { return queued - landed; },
    cancel: function () { queue.cancel(); },
    sync: function () {
      const on = opts.chiefOn();
      // A running batch keeps the button live: a tap on it cancels the batch.
      button.disabled = !on && !queue.busy();
      note.hidden = on;
    },
  };
  live.push(control);

  wireFilePicker(button, picker, queue, add);

  label(false);
  paintChips();
  control.sync();
  return {
    button: button,
    extra: extra,
    wirePaste: function (input) { wirePasteInto(input, add); },
  };
}
