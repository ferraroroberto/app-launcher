/* Life OS content browser: a read-only sheet of one skill's files —
 * public SKILL.md/description.md plus
 * the private context/memory/examples/conversations — and the document
 * sheet that opens one. A conversation log (disposable, unlike every other
 * category) can be renamed or deleted from here; the shared Rename sheet it
 * uses (`askName`) is also the one the conversation title rename in
 * life-os-convos.js reuses, so there is exactly one rename dialog on the tab.
 *
 * Those content endpoints are Tailscale + passkey gated server-side, so a
 * fetch may 403; we surface the reason rather than a blank pane.
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { nameLabel } from '../dom-utils.js';
import { renderMarkdown } from '../markdown.js';
import { confirmDialog } from '../confirm-dialog.js';
import { createSettler } from '../modal-settle.js';
import { refreshAfterLogChange, wireSheet } from '../life-os.js';
import { closeConvoLayersForPath } from './life-os-convos.js';

// The file the document sheet shows, which its Rename log and Delete log
// rows act on. Null while the sheet is closed.
let openDocFile = null;

export async function openBrowser(s) {
  const d = els.lifeOsBrowser;
  if (!d) return;
  state.lifeOsBrowser = { skillId: s.id, name: s.name, files: [] };
  els.lifeOsBrowserTitle.textContent = s.name;
  if (!d.open) d.showModal();
  await loadFileList();
}

// (Re)load the current skill's file list — runs every time the browser
// opens, so a conversation log added on the PC shows up on reopen.
async function loadFileList() {
  const b = state.lifeOsBrowser;
  if (!b) return;
  els.lifeOsFileList.innerHTML = '<li class="muted small">Loading…</li>';
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(b.skillId) + '/files'
    );
    b.files = body.files || [];
    renderFileList(b.files);
  } catch (exc) {
    // The content endpoints are Tailscale + passkey gated — a 403 here
    // means this connection can't reach them. Say so plainly, in the list.
    const msg = (exc && exc.status === 403)
      ? 'The content browser is Tailscale-only (and passkey-gated). Open the ' +
        'launcher over your Tailscale URL on an enrolled device.'
      : 'Could not load files: ' + (exc.message || exc);
    els.lifeOsFileList.innerHTML = '';
    const li = document.createElement('li');
    li.className = 'muted small';
    li.textContent = msg;
    els.lifeOsFileList.appendChild(li);
  }
}

// Reloads the browse list only while its sheet is open — the file-list half
// of life-os.js's refreshAfterLogChange (the Conversations group is the
// other half, in life-os-convos.js).
export function refreshFileListIfOpen() {
  if (els.lifeOsBrowser && els.lifeOsBrowser.open) return loadFileList();
  return Promise.resolve();
}

function renderFileList(files) {
  const host = els.lifeOsFileList;
  host.innerHTML = '';
  if (!files.length) {
    const p = document.createElement('li');
    p.className = 'muted small';
    p.textContent = 'No readable files.';
    host.appendChild(p);
    return;
  }
  // The server sorts by category, so each category is one contiguous run.
  const runs = [];
  files.forEach(function (f) {
    const last = runs[runs.length - 1];
    if (last && last.category === f.category) last.files.push(f);
    else runs.push({ category: f.category, files: [f] });
  });
  runs.forEach(function (run) {
    const h = document.createElement('li');
    h.className = 'lifeos-file-cat';
    h.textContent = nameLabel(run.category);
    host.appendChild(h);
    renderCategory(host, run);
  });
}

// A category's top-level files first, then one collapsed row per sub-folder
// (machine output such as a skill's generated history sits there), so the
// curated files are on the first screen. A category with no sub-folder
// renders exactly as a flat list.
function renderCategory(host, run) {
  const top = [];
  const folders = new Map();
  run.files.forEach(function (f) {
    const name = String(f.name || '');
    // The server labels a nested file with its OS path separator.
    const slash = name.search(/[\\/]/);
    if (slash <= 0) { top.push(f); return; }
    const folder = name.slice(0, slash);
    if (!folders.has(folder)) folders.set(folder, []);
    folders.get(folder).push({ file: f, label: name.slice(slash + 1) });
  });
  top.forEach(function (f) { host.appendChild(fileRow(f, f.name, false)); });
  folders.forEach(function (entries, folder) {
    const key = run.category + '/' + folder;
    const open = openFolders().has(key);
    const rows = entries.map(function (e) {
      const row = fileRow(e.file, e.label, true);
      row.hidden = !open;
      return row;
    });
    host.appendChild(folderRow(key, folder, rows, open));
    rows.forEach(function (row) { host.appendChild(row); });
  });
}

// The folders the reader has expanded, kept on the open browser so a reload
// of the list (a log added or deleted) does not fold them again.
function openFolders() {
  const b = state.lifeOsBrowser;
  if (!b.openFolders) b.openFolders = new Set();
  return b.openFolders;
}

function folderRow(key, folder, rows, open) {
  const li = document.createElement('li');
  li.className = 'lifeos-file-row';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'lifeos-folder-btn';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  const label = document.createElement('span');
  label.className = 'lifeos-folder-label';
  label.textContent = folder + ' · ' + rows.length + (rows.length === 1 ? ' file' : ' files');
  const chevron = document.createElement('span');
  chevron.className = 'lifeos-folder-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.innerHTML =
    '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-chevron-right"></use></svg>';
  btn.appendChild(label);
  btn.appendChild(chevron);
  btn.addEventListener('click', function () {
    const nowOpen = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
    if (nowOpen) openFolders().add(key); else openFolders().delete(key);
    rows.forEach(function (row) { row.hidden = !nowOpen; });
  });
  li.appendChild(btn);
  return li;
}

function fileRow(f, label, nested) {
  const li = document.createElement('li');
  li.className = nested ? 'lifeos-file-row lifeos-file-nested' : 'lifeos-file-row';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'lifeos-file-btn';
  btn.textContent = label;
  btn.title = f.path;
  btn.addEventListener('click', function () { loadFile(f); });
  li.appendChild(btn);
  // No delete control in the list — the list is navigation only. A
  // disposable conversation log's Rename and Delete sit under its text
  // in the document sheet (see loadFile below).
  return li;
}

async function deleteFile(f) {
  const ok = await confirmDialog({
    title: 'Delete this conversation log?',
    message: f.name + ' is removed from disk. This cannot be undone.',
    action: 'Delete',
  });
  if (!ok) return;
  try {
    await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path),
      { method: 'DELETE' }
    );
  } catch (exc) {
    apiFailToast('Delete failed', exc);
    return;
  }
  toast('Deleted ' + f.name, 'good', { icon: 'trash-2' });
  closeDoc();
  // The conversation it was the capture of is gone with it.
  closeConvoLayersForPath(f.path);
  await refreshAfterLogChange();
}

// Lower-case, spaces (and any other punctuation) → single dashes, trimmed —
// the same shape the capture hook's slugs already have.
function slugify(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

// The name part of a log's file name: what follows the date-and-time prefix
// the server keeps on a rename (life_os_files.py _DATE_PREFIX_RE).
function logSlug(name) {
  return String(name || '').replace(/\.md$/i, '')
    .replace(/^\d{4}-\d{2}-\d{2}-\d{4}-/, '');
}

// The rename sheet, in place of a native prompt(), resolves the name
// typed, or null when ✕, Escape or a backdrop tap cancels it.
const renameSettler = createSettler(function () { return els.lifeOsRenameDialog; }, null);
const finishRename = renameSettler.finish;

// The one sheet serves both renames; each fills in its own wording. The log
// rename (a file name, always required) is the original; the conversation
// title (set from life-os-convos.js) may be left empty, which clears it.
const RENAME_LOG_SHEET = {
  heading: 'Rename log', action: 'Rename', placeholder: '', required: true,
  help: 'The date stays. Spaces become dashes, lower-cased.',
  maxLength: null, typing: 'none',
};
export const RENAME_TITLE_SHEET = {
  heading: 'Rename conversation', action: 'Save', required: false,
  help: 'Leave it empty to go back to the topic.',
  maxLength: 120, typing: 'sentences', selectAll: true,
};

export function askName(current, sheet) {
  const d = els.lifeOsRenameDialog;
  if (!d) return Promise.resolve(null);
  renameSettler.cancelPending();
  const input = els.lifeOsRenameInput;
  els.lifeOsRenameHeading.textContent = sheet.heading;
  els.lifeOsRenameHelp.textContent = sheet.help;
  els.lifeOsRenameSave.textContent = sheet.action;
  input.required = sheet.required;
  input.placeholder = sheet.placeholder;
  if (sheet.maxLength) input.maxLength = sheet.maxLength;
  else input.removeAttribute('maxlength');
  input.setAttribute('autocapitalize', sheet.typing);
  input.spellcheck = sheet.typing !== 'none';
  input.value = current;
  return renameSettler.show(d, function () {
    // A pre-filled starting point is selected, so typing replaces it and the
    // arrow keys edit it.
    if (sheet.selectAll) { input.focus(); input.select(); }
  });
}

function wireRenameDialog() {
  const d = els.lifeOsRenameDialog;
  if (!d) return;
  els.lifeOsRenameClose.addEventListener('click', function () { finishRename(null); });
  els.lifeOsRenameForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    finishRename(els.lifeOsRenameInput.value);
  });
  renameSettler.wireDismiss(d);
}

async function renameFile(f) {
  const proposed = await askName(logSlug(f.name), RENAME_LOG_SHEET);
  if (proposed === null) return;            // cancelled
  const slug = slugify(proposed);
  if (!slug) { toast('Name cannot be empty', 'error'); return; }
  let body;
  try {
    body = await jsonApi('/api/life-os/file/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: f.path, slug: slug }),
    });
  } catch (exc) {
    apiFailToast('Rename failed', exc);
    return;
  }
  toast('Renamed to ' + (body.name || slug), 'good', { icon: 'pencil' });
  // Name (and path) changed: back to the list, and a conversation sheet
  // still holding the old path has nothing left to act on.
  closeDoc();
  closeConvoLayersForPath(f.path);
  await refreshAfterLogChange();
}

export async function loadFile(f) {
  const d = els.lifeOsDoc;
  if (!d) return;
  openDocFile = f;
  els.lifeOsDocTitle.textContent = f.name || 'File';
  // Rename and Delete only for a conversation log — disposable run
  // transcripts, editable while you read them. Every other category (and
  // the .gitkeep placeholder) only reads.
  els.lifeOsDocLinks.hidden = !isEditableLog(f);
  els.lifeOsFileContent.innerHTML = '<p class="muted small">Loading…</p>';
  if (!d.open) d.showModal();
  try {
    const body = await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path)
    );
    if (openDocFile !== f) return;          // closed or moved on meanwhile
    els.lifeOsFileContent.innerHTML = renderMarkdown(body.content || '');
    if (body.truncated) {
      const note = document.createElement('p');
      note.className = 'muted small';
      note.textContent = '… (truncated)';
      els.lifeOsFileContent.appendChild(note);
    }
    els.lifeOsFileContent.scrollTop = 0;
  } catch (exc) {
    if (openDocFile !== f) return;
    els.lifeOsFileContent.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'Could not load: ' + (exc.message || exc);
    els.lifeOsFileContent.appendChild(p);
  }
}

// A conversation log the sheet may act on — any file under a skill's
// conversations/ EXCEPT the .gitkeep placeholder that keeps the (otherwise
// empty) dir tracked in git. Deleting/renaming that would untrack the dir,
// so it stays off-limits (the server refuses it too — defence in depth).
function isEditableLog(f) {
  return !!f && f.category === 'conversations' &&
    !/(^|\/)\.gitkeep$/.test(f.name || '');
}

export function closeDoc() {
  if (els.lifeOsDoc && els.lifeOsDoc.open) els.lifeOsDoc.close();
}

export function wireContentBrowser() {
  // Browse and its document sheet: ✕ or Done closes each, back to
  // the one under it.
  wireSheet(els.lifeOsBrowser, [els.lifeOsBrowserClose, els.lifeOsBrowserDone],
    function () { state.lifeOsBrowser = null; });
  wireSheet(els.lifeOsDoc, [els.lifeOsDocClose, els.lifeOsDocDone], function () {
    openDocFile = null;
    els.lifeOsFileContent.innerHTML = '';
  });
  if (els.lifeOsDocDelete) {
    els.lifeOsDocDelete.addEventListener('click', function () {
      if (openDocFile) deleteFile(openDocFile);
    });
  }
  if (els.lifeOsDocRename) {
    els.lifeOsDocRename.addEventListener('click', function () {
      if (openDocFile) renameFile(openDocFile);
    });
  }
  wireRenameDialog();
}
