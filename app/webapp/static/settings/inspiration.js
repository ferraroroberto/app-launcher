/* Inspiration: Settings > Inspiration. The daily Inspiration scan's source
 * list (newsletters, YouTube channels, X accounts, a note on what you like),
 * the latest digest's status and the one-time X login.
 *
 * GET /api/inspiration/sources -> the list, a `revision` and the digest status;
 * PUT sends the whole list back with that revision and returns the same shape.
 * The inspiration-scan skill owns the file and its format, so the server
 * validates and writes it; this module draws what it returns and shows its
 * message under the field when a save is refused. A row keeps the `index` it
 * came with, so a key the sheet does not show survives an edit.
 *
 * The X login is a window on the PC, started by POST /api/inspiration/login-x.
 * The button polls its state while the window is open and says when it closed.
 */

import { els } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { ACTION_ROW_MENU, actionRow } from '../action-rows.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';
import { confirmDialog } from '../confirm-dialog.js';

const LOGIN_POLL_MS = 2000;
const KINDS = ['newsletter', 'youtube', 'x'];
const KIND_NOUN = { newsletter: 'newsletter', youtube: 'channel', x: 'account' };

const rowMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });

let current = null;       // the last server state
let editing = null;       // { kind, index } of the open form; index null = adding
let loginTimer = 0;

function body() { return els.inspirationBody; }
function byId(id) { return document.getElementById(id); }
function group(kind) { return body().querySelector('.opt-group[data-kind="' + kind + '"]'); }

function setRowValue(text) {
  document.querySelectorAll('[data-settings-value="inspiration"]').forEach(function (el) {
    el.textContent = text;
  });
}

function sourceCount(s) { return s.newsletter.length + s.youtube.length + s.x.length; }

function rowValue(s) {
  if (!s || !s.available) return 'Unavailable';
  const n = sourceCount(s);
  return n === 1 ? '1 source' : n + ' sources';
}

// ------------------------------------------------------------- drawing
function digestLine(d) {
  if (!d || d.state === 'none') return 'No digest yet. The first one appears after the next scan.';
  if (d.state === 'unfilled') return d.date + ': the scan ran before any source was listed.';
  if (d.state === 'unreadable') return d.date + ': the latest digest could not be read.';
  return d.date + ' · ' + d.kept + (d.kept === 1 ? ' idea' : ' ideas') + ' kept from ' + d.items + ' items';
}

function drawDigest(d) {
  byId('inspirationDigest').textContent = digestLine(d);
  const lines = byId('inspirationDigestLines');
  lines.textContent = '';
  ((d && d.chips) || []).forEach(function (chip) {
    const li = document.createElement('li');
    li.textContent = chip.text;
    if (!chip.ok) li.dataset.tone = 'attention';
    lines.appendChild(li);
  });
}

function rowParts(kind, entry) {
  if (kind === 'newsletter') return { title: entry.name || 'Unnamed newsletter', meta: entry.feed };
  if (kind === 'youtube') return { title: entry.name || 'Unnamed channel', meta: entry.channel_id };
  return { title: '@' + entry.handle, meta: '' };
}

function drawKind(kind) {
  const g = group(kind);
  const list = g.querySelector('[data-list]');
  list.textContent = '';
  current[kind].forEach(function (entry) {
    const parts = rowParts(kind, entry);
    const noun = KIND_NOUN[kind];
    const row = actionRow({
      id: kind + ':' + entry.index,
      title: parts.title,
      meta: parts.meta,
      label: 'Edit ' + parts.title,
      onMain: function () { openForm(kind, entry); },
      kebabLabel: 'More for ' + parts.title,
    });
    rowMenu.attach(kind + ':' + entry.index, row.kebab, [
      { label: 'Edit', glyph: 'pencil', onTap: function () { openForm(kind, entry); } },
      { label: 'Remove', glyph: 'trash-2', danger: true, onTap: function () { remove(kind, entry, noun); } },
    ]);
    list.appendChild(row.li);
  });
  rowMenu.endRender();
}

function draw(state) {
  current = state;
  const ready = !!(state && state.available);
  byId('inspirationUnavailable').hidden = ready;
  byId('inspirationReason').textContent = ready ? '' : ((state && state.reason) || 'The source list could not be read.');
  byId('inspirationContent').hidden = !ready;
  setRowValue(rowValue(state));
  if (!ready) return;
  const problem = byId('inspirationProblem');
  problem.hidden = !state.problem;
  problem.textContent = state.problem || '';
  drawDigest(state.digest);
  byId('inspirationEditor').hidden = !state.editable;
  KINDS.forEach(function (kind) {
    group(kind).hidden = !state.editable;
    if (state.editable) drawKind(kind);
  });
  byId('inspirationLikes').value = state.likes || '';
}

// --------------------------------------------------------------- saving
function payload(overrides) {
  const out = {
    revision: current.revision,
    likes: byId('inspirationLikes').value,
    newsletter: current.newsletter, youtube: current.youtube, x: current.x,
  };
  return Object.assign(out, overrides);
}

async function put(overrides) {
  const next = await jsonApi('/api/inspiration/sources', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload(overrides)),
  });
  closeForm();
  draw(next);
  return next;
}

function showError(el, message) {
  el.textContent = message || '';
  el.hidden = !message;
}

async function saveLikes() {
  const error = byId('inspirationLikesError');
  showError(error, '');
  try {
    await put({ likes: byId('inspirationLikes').value });
    toast('Note saved.', 'good');
  } catch (exc) {
    showError(error, exc.message);
  }
}

async function remove(kind, entry, noun) {
  const parts = rowParts(kind, entry);
  const ok = await confirmDialog({
    title: 'Remove ' + noun + '?',
    message: parts.title + ' leaves the scan list. You can add it again later.',
    action: 'Remove',
  });
  if (!ok) return;
  const rest = current[kind].filter(function (e) { return e.index !== entry.index; });
  const overrides = {};
  overrides[kind] = rest;
  try {
    await put(overrides);
    toast('Removed.', 'good');
  } catch (exc) {
    apiFailToast('Remove failed', exc);
  }
}

// ---------------------------------------------------------------- forms
function formOf(kind) { return group(kind).querySelector('[data-form]'); }

function closeForm() {
  if (!editing) return;
  const g = group(editing.kind);
  g.querySelector('[data-form]').hidden = true;
  g.querySelector('[data-add-row]').hidden = false;
  editing = null;
}

function openForm(kind, entry) {
  closeForm();
  const form = formOf(kind);
  form.reset();
  Array.prototype.forEach.call(form.elements, function (field) {
    if (field.name) field.value = entry ? (entry[field.name] || '') : '';
  });
  showError(form.querySelector('[data-error]'), '');
  // Settings has no Save button: the form's action says what it does to the list.
  form.querySelector('[data-submit]').textContent = entry ? 'Update' : 'Add';
  form.hidden = false;
  group(kind).querySelector('[data-add-row]').hidden = true;
  editing = { kind: kind, index: entry ? entry.index : null };
  const first = form.elements[0];
  if (first) first.focus();
}

async function submitForm(kind, form) {
  const fields = {};
  Array.prototype.forEach.call(form.elements, function (field) {
    if (field.name) fields[field.name] = field.value;
  });
  const rows = current[kind].map(function (e) { return e; });
  if (editing.index == null) {
    rows.push(fields);
  } else {
    const at = rows.findIndex(function (e) { return e.index === editing.index; });
    rows[at] = Object.assign({}, rows[at], fields);
  }
  const overrides = {};
  overrides[kind] = rows;
  try {
    await put(overrides);
    toast('Saved.', 'good');
  } catch (exc) {
    showError(form.querySelector('[data-error]'), exc.message);
  }
}

// ---------------------------------------------------------------- login
function loginText(s) {
  if (s.state === 'running') return 'A Chrome window is open on this PC. Log into X there, then close the window.';
  if (s.state === 'closed') return 'The login window was closed. X is read at the next scan.';
  if (s.state === 'failed') return 'The login window did not complete' + (s.detail ? ': ' + s.detail : '.');
  return '';
}

function drawLogin(s) {
  const btn = byId('inspirationLoginBtn');
  btn.disabled = s.state === 'running';
  byId('inspirationLoginStatus').textContent = loginText(s);
  window.clearTimeout(loginTimer);
  if (s.state === 'running') loginTimer = window.setTimeout(pollLogin, LOGIN_POLL_MS);
}

async function pollLogin() {
  try {
    drawLogin(await jsonApi('/api/inspiration/login-x'));
  } catch (_) {
    loginTimer = window.setTimeout(pollLogin, LOGIN_POLL_MS);
  }
}

async function startLogin() {
  try {
    drawLogin(await jsonApi('/api/inspiration/login-x', { method: 'POST' }));
  } catch (exc) {
    byId('inspirationLoginStatus').textContent = exc.message;
  }
}

// ----------------------------------------------------------------- load
async function load(quiet) {
  try {
    draw(await jsonApi('/api/inspiration/sources'));
  } catch (exc) {
    draw({ available: false, reason: 'The source list could not be read.' });
    if (!quiet) apiFailToast('Inspiration', exc);
  }
}

export function wireInspiration() {
  if (!body()) return;
  KINDS.forEach(function (kind) {
    const g = group(kind);
    const form = formOf(kind);
    g.querySelector('[data-add]').addEventListener('click', function () { openForm(kind, null); });
    g.querySelector('[data-cancel]').addEventListener('click', closeForm);
    form.addEventListener('submit', function (ev) { ev.preventDefault(); submitForm(kind, form); });
  });
  byId('inspirationLikesSave').addEventListener('click', saveLikes);
  byId('inspirationLoginBtn').addEventListener('click', startLogin);
  // Read on each visit to Settings and on opening the sheet, so the row's
  // value and the digest line are never older than the file.
  document.querySelectorAll('[data-sheet="inspirationSheet"]').forEach(function (btn) {
    btn.addEventListener('click', function () { closeForm(); load(false); pollLogin(); });
  });
  byId('inspirationSheet').addEventListener('close', function () {
    window.clearTimeout(loginTimer);
    closeForm();
  });
  document.addEventListener('launcher:tab', function (e) {
    if (e.detail && e.detail.tab === 'settings') load(true);
  });
}
