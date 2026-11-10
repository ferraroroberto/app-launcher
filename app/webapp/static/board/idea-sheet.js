/* One inspiration idea's sheet, opened from the Board's Ideas card.
 *
 * It reads the idea whole: the title, the score, the excerpt, where it came
 * from (author, date, link, digest, the model that scored it), why it fits and
 * the experiment. An undecided idea adds the decision: the repo to file in
 * (the scan's suggestion first), the long /issue-add prompt to edit, files
 * (the answer sheets' paperclip and image paste, answer-files.js) and
 * dictation into the prompt or the note (voice.js), then File it, Drop it or
 * Later (which only closes).
 *
 * File and Drop each send the chief one message, the contract in fleet-config's
 * docs/inspiration-ideas.md, through the path the answer sheets use
 * (board-dispatch.js sendToChief):
 *   Idea from the Board: <id> -> file in <repo>   (or "-> drop")
 *   <the edited prompt, or the note>
 *   Attached: <path>                              (one per file)
 * The chief files the issue and records the outcome in the ledger. Until that
 * shows on the card, the idea reads "sent" here and offers no second send. A
 * decided idea shows its outcome instead of the actions. A failed send keeps
 * the draft, which stays in memory per idea across opens until it is sent.
 */

import { apiFailToast, toast } from '../api.js';
import { state } from '../state.js';
import { el } from '../dom-utils.js';
import { icon } from '../_vendored/icons/icons.js';
import { chip } from '../glance.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from '../voice.js';
import { chiefSessionId, sendToChief } from './board-dispatch.js';
import { uploadToChief } from './board-answers.js';
import { disposeAnswerFiles, filesUploading, mountAnswerFiles, syncAnswerFiles } from './answer-files.js';
import { ANSWER_TEXT_MAX } from './answer-item.js';

const KIND_LABELS = { newsletter: 'Newsletter', youtube: 'YouTube', x: 'X' };
const FILE_LABEL = 'File it';
const DROP_LABEL = 'Drop it';

// The open sheet: { entry, opts, draft }, or null.
let sheet = null;
let sending = false;
let chiefRun = '';
let dictations = [];
// Drafts by idea id, kept across opens until their message is sent.
const drafts = {};

function byId(id) { return document.getElementById(id); }

// ------------------------------------------------------------ helpers

export function kindLabel(kind) {
  return KIND_LABELS[kind] || kind || 'Source';
}

// "8 Nov" from an ISO date or time; '' when there is none to read.
export function shortDate(iso) {
  const t = Date.parse(iso || '');
  if (isNaN(t)) return '';
  return new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

export function scoreText(score) {
  return typeof score === 'number' ? score + '/10' : '';
}

// Who it came from: the author, or the source's name when the feed has none.
export function sourceText(entry) {
  const source = entry.source || {};
  return kindLabel(source.kind) + ' · ' + (entry.author || source.name || 'unknown');
}

// The one message File or Drop sends the chief (fleet-config
// docs/inspiration-ideas.md). The body goes as typed, trimmed; an empty note
// adds no line. Each file is one `Attached:` line after it.
export function composeIdeaMessage(id, action, repo, text, files) {
  const head = 'Idea from the Board: ' + id + ' -> ' + (action === 'file' ? 'file in ' + repo : 'drop');
  const lines = [head];
  const body = String(text || '').trim();
  if (body) lines.push(body);
  (files || []).forEach(function (f) { lines.push('Attached: ' + f.path); });
  return lines.join('\n');
}

// The repos the picker offers: the Code tab's projects, plus the scan's
// suggestion when it is not among them.
export function repoChoices(projects, suggested) {
  const names = projects.slice().sort();
  if (suggested && names.indexOf(suggested) === -1) names.unshift(suggested);
  return names;
}

// ------------------------------------------------------------- render

function section(title) {
  return el('p', 'idea-sheet-section', title);
}

function attribution(entry) {
  const dl = el('dl', 'idea-sheet-attr');
  const add = function (term, value) {
    if (!value) return;
    dl.appendChild(el('dt', '', term));
    const dd = el('dd', '');
    if (typeof value === 'string') dd.textContent = value; else dd.appendChild(value);
    dl.appendChild(dd);
  };
  add('Author', sourceText(entry));
  add('Posted', shortDate(entry.published));
  if (entry.link) {
    const a = el('a', 'idea-sheet-link', entry.link.replace(/^https?:\/\//, ''));
    a.href = entry.link;
    a.target = '_blank';
    a.rel = 'noopener';
    a.dataset.track = 'idea-sheet.source-link';
    add('Link', a);
  }
  const found = ['digest ' + (entry.digest || 'unknown'), entry.model ? 'scored by ' + entry.model : '']
    .filter(Boolean).join(' · ');
  add('Found by', 'inspiration scan · ' + found);
  return dl;
}

function headBlock(entry) {
  const nodes = [el('h3', 'idea-sheet-title', entry.title || entry.link || 'Untitled idea')];
  const chips = el('div', 'idea-sheet-chips');
  if (scoreText(entry.score)) chips.appendChild(chip(scoreText(entry.score), 'neutral', 'idea-sheet-score'));
  chips.appendChild(chip(kindLabel((entry.source || {}).kind), 'neutral'));
  nodes.push(chips);
  if (entry.excerpt) nodes.push(el('blockquote', 'idea-sheet-quote', entry.excerpt));
  nodes.push(attribution(entry));
  if (entry.why_fits) nodes.push(section('Why it fits'), el('p', 'idea-sheet-text', entry.why_fits));
  if (entry.experiment) nodes.push(section('Try first'), el('p', 'idea-sheet-text', entry.experiment));
  return nodes;
}

// A decided idea, or one sent from this page: what became of it.
function outcomeBlock(entry, sentAction) {
  const box = el('div', 'idea-sheet-outcome');
  if (entry.state === 'applied') {
    box.appendChild(el('p', 'idea-sheet-text', 'Filed as an issue on ' + (shortDate(entry.at) || 'an earlier day') + '.'));
    if (entry.issue_url) {
      const a = el('a', 'chip idea-sheet-issue', entry.issue);
      a.href = entry.issue_url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.dataset.track = 'idea-sheet.issue';
      box.appendChild(a);
    } else if (entry.issue) {
      box.appendChild(chip(entry.issue, 'neutral', 'idea-sheet-issue'));
    }
  } else if (entry.state === 'filtered') {
    box.appendChild(el('p', 'idea-sheet-text', 'Dropped on ' + (shortDate(entry.at) || 'an earlier day') + '.'));
    if (entry.note) box.appendChild(el('blockquote', 'idea-sheet-quote', entry.note));
  } else {
    box.appendChild(el('p', 'idea-sheet-text', (sentAction === 'file' ? 'Sent to the chief to file.'
      : 'Sent to the chief to drop.') + ' Waiting for it to record the outcome.'));
  }
  return box;
}

// A dictation button that types into `field`.
function micFor(field, label) {
  const mic = el('button', 'icon-button compose-record tr-ask-mic');
  mic.type = 'button';
  mic.title = 'Dictate (voice to text)';
  mic.setAttribute('aria-label', label);
  mic.setAttribute('aria-pressed', 'false');
  mic.dataset.track = 'idea-sheet.dictate';
  mic.innerHTML = icon('mic');
  const dictation = createDictation({
    button: mic,
    getTextarea: function () { return field; },
    onRender: function () { field.dispatchEvent(new Event('input')); },
  });
  mic.addEventListener('click', dictation.toggle);
  dictations.push(dictation);
  return mic;
}

function repoPicker(draft, suggested) {
  const projects = (state.apps || [])
    .filter(function (a) { return a.kind === 'claude-code'; })
    .map(function (a) { return String(a.name); });
  const select = el('select', 'select-native idea-sheet-repo');
  select.setAttribute('aria-label', 'Repo to file the issue in');
  select.dataset.track = 'idea-sheet.repo';
  select.appendChild(new Option('Pick a repo', ''));
  repoChoices(projects, suggested).forEach(function (name) {
    select.appendChild(new Option(name + (name === suggested ? ' (suggested)' : ''), name));
  });
  select.value = draft.repo;
  select.addEventListener('change', function () {
    draft.repo = select.value;
    syncActions();
  });
  return select;
}

function decisionBlock(entry, draft) {
  const nodes = [section('File as issue in'), repoPicker(draft, entry.suggested_repo)];
  nodes.push(section('/issue-add prompt'));
  const prompt = el('textarea', 'input-native idea-sheet-prompt');
  prompt.rows = 12;
  prompt.maxLength = ANSWER_TEXT_MAX;
  prompt.value = draft.prompt;
  prompt.setAttribute('aria-label', 'The /issue-add prompt');
  prompt.addEventListener('input', function () {
    draft.prompt = prompt.value;
    syncActions();
  });
  nodes.push(prompt);
  const files = mountAnswerFiles({
    template: byId('ideaAttachTemplate'),
    draft: draft,
    label: 'the idea',
    chiefOn: function () { return !!chiefSessionId(); },
    upload: uploadToChief,
    sheetUsed: function () { return draft.files.length; },
    onChange: syncActions,
  });
  files.wirePaste(prompt);
  const tools = el('div', 'idea-sheet-tools');
  tools.appendChild(el('span', 'muted small', 'Screenshots or a voice note'));
  if (voiceDictationAvailable()) tools.appendChild(micFor(prompt, 'Dictate into the prompt'));
  tools.appendChild(files.button);
  nodes.push(tools, files.extra);

  nodes.push(section('Note, if you drop it'));
  const noteRow = el('div', 'tr-ask-other');
  const note = el('input', 'tr-ask-input idea-sheet-drop-note');
  note.type = 'text';
  note.maxLength = ANSWER_TEXT_MAX;
  note.placeholder = 'Why not (optional)';
  note.value = draft.note;
  note.setAttribute('aria-label', 'Why you drop it (optional)');
  note.addEventListener('input', function () { draft.note = note.value; });
  files.wirePaste(note);
  noteRow.appendChild(note);
  if (voiceDictationAvailable()) noteRow.appendChild(micFor(note, 'Dictate the note'));
  nodes.push(noteRow);
  return nodes;
}

function draftFor(entry) {
  if (!drafts[entry.id]) {
    drafts[entry.id] = { repo: entry.suggested_repo || '', prompt: entry.prompt || '', note: '', files: [] };
  }
  return drafts[entry.id];
}

function decided(entry) {
  return !!entry.state || !!(sheet && sheet.opts.sent[entry.id]);
}

// File needs a repo and a prompt; both need the chief running, as the answer
// sheets' Done does. The note says why a button is off.
function syncActions() {
  if (!sheet) return;
  const actions = byId('ideaSheetActions');
  actions.hidden = decided(sheet.entry);
  if (actions.hidden) return;
  const draft = sheet.draft;
  let chiefWhy = '';
  if (chiefRun !== 'running') {
    chiefWhy = chiefRun === 'unknown' ? 'Chief status unknown — session-host unreachable.'
      : 'Chief not running. Start it to send this idea.';
  }
  let fileWhy = chiefWhy;
  if (!fileWhy && !draft.repo) fileWhy = 'Pick a repo to file in.';
  if (!fileWhy && !draft.prompt.trim()) fileWhy = 'The prompt is empty.';
  byId('ideaSheetFile').disabled = sending || !!fileWhy;
  byId('ideaSheetDrop').disabled = sending || !!chiefWhy;
  const note = byId('ideaSheetNote');
  note.textContent = fileWhy;
  note.hidden = !fileWhy;
}

function disposeSheet() {
  dictations.forEach(function (d) { d.dispose(); });
  dictations = [];
  disposeAnswerFiles();
}

// ------------------------------------------------------------- public

// `opts`: { run, sent (id -> action, this page's sends), onSent(id, action) }.
export function openIdeaSheet(entry, opts) {
  const dialog = byId('ideaSheetDialog');
  if (!dialog) return;
  disposeSheet();
  chiefRun = opts.run || chiefRun;
  sheet = { entry: entry, opts: opts, draft: entry.state ? null : draftFor(entry) };
  const nodes = headBlock(entry);
  if (decided(entry)) nodes.push(outcomeBlock(entry, opts.sent[entry.id]));
  else nodes.push.apply(nodes, decisionBlock(entry, sheet.draft));
  const body = byId('ideaSheetBody');
  body.replaceChildren.apply(body, nodes);
  syncActions();
  if (!dialog.open) dialog.showModal();
}

// Each Board render: whether the chief runs, for the open sheet's buttons.
export function syncIdeaSheet(run) {
  chiefRun = run || '';
  if (!sheet) return;
  syncAnswerFiles();
  syncActions();
}

async function send(action) {
  if (!sheet || sending || decided(sheet.entry)) return;
  if (dictations.some(function (d) { return d.isBusy(); })) {
    toast('Still transcribing — wait for the transcript, then send', 'error', { icon: 'mic' });
    return;
  }
  if (filesUploading()) {
    toast('Still uploading — wait for the files, then send', 'error', { icon: 'paperclip' });
    return;
  }
  const { entry, opts, draft } = sheet;
  const button = byId(action === 'file' ? 'ideaSheetFile' : 'ideaSheetDrop');
  sending = true;
  syncActions();
  const stopTimer = startWorkTimer(button, action === 'file' ? FILE_LABEL : DROP_LABEL);
  try {
    await sendToChief(composeIdeaMessage(entry.id, action, draft.repo,
      action === 'file' ? draft.prompt : draft.note, draft.files));
    delete drafts[entry.id];
    opts.onSent(entry.id, action);
    byId('ideaSheetDialog').close();
    toast('Sent to the chief', 'good', { icon: 'crown' });
  } catch (exc) {
    // The sheet stays open on the draft, to send again.
    apiFailToast('Not sent to the chief', exc);
  } finally {
    stopTimer();
    sending = false;
    syncActions();
  }
}

export function wireIdeaSheet() {
  const dialog = byId('ideaSheetDialog');
  if (!dialog) return;
  const close = function () { dialog.close(); };
  byId('ideaSheetClose').addEventListener('click', close);
  byId('ideaSheetLater').addEventListener('click', close);
  byId('ideaSheetFile').addEventListener('click', function () { send('file'); });
  byId('ideaSheetDrop').addEventListener('click', function () { send('drop'); });
  dialog.addEventListener('close', function () {
    disposeSheet();
    sheet = null;
  });
}
