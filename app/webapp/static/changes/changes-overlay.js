/* "Show changes" overlay: a read-only, phone-first view of one
 * project's working tree — what a red (dirty) Coding tile actually means,
 * without opening VS Code just to look. The same panel also
 * shows a session's **Changed files** (the session ⋮ menu): every file that
 * session's edits touched, folded from its transcript rather than git, so it
 * still works once the work is committed or the worktree is gone.
 *
 * Shape borrowed from GitHub's "Files changed": each file a collapsible row
 * (status badge · path · +N −N) whose diff loads lazily on first open. A
 * project's diff is git's unified text, tinted per line (`+` / `-` / `@@`)
 * by a tiny renderer below; a session's is each of its own edits in order,
 * drawn by diff-view.js. No library. A viewer only — nothing here stages,
 * commits or discards.
 *
 * A row reads name first, its folder as the hint (the row the
 * Chat pane's Edited N files card shares, edited-card.js), and a session's
 * panel can open from that card: focused on one file, with a "This turn ·
 * Whole session" pair that starts on the turn. The turn's scope is drawn from
 * the steps the Chat page already holds, so it costs no request.
 *
 * An opened session file has a Diff | File switch: File is the
 * file as it is on disk now, drawn for its type by file-view.js, read through
 * a route that serves only files the session edited. Beside it, Download
 * and a ⋮ menu: Copy content in File view, Copy diff in Diff view,
 * Copy path, and on the PC itself Open on PC (VS Code, else Explorer). Still
 * read-only: nothing edits, stages or discards.
 */

import { els } from '../state.js';
import { apiFailToast, escapeHtml, jsonApi, toast } from '../api.js';
import { icon } from '../_vendored/icons/icons.js';
import { diffTint, renderHunks } from './diff-view.js';
import { tintDiffLines } from './syntax-tint.js';
import { terminalJsonApi } from '../webauthn.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';
import { fileBadge, fileCounts, fileKey, fileName } from '../chat/edited-card.js';
import { renderFileView, windowsPath } from './file-view.js';
import { onThePc } from '../chat/transcript-render.js';

// Why a session's Changed files could not be read, in the panel's words.
const SESSION_REASON = {
  no_transcript: 'No transcript for this session',
  unsupported_agent: 'Changed files is not available for this agent',
  session_not_found: 'This session has ended',
  session_host_unreachable: 'The session host is unreachable',
};

// Why the File view can't show a file, one sentence per reason.
const FILE_REASON = {
  file_not_found: 'This session didn’t edit this file',
  deleted: 'This file is no longer on disk',
  not_text: 'Not a text file',
  read_failed: 'Couldn’t read this file',
};

// null while closed, else what is shown: a project ({kind: 'project', id,
// name}) or a session ({kind: 'session', sid, name, project}). `seq` guards a slow
// response from an earlier open landing in a newer view. A session opened
// from an Edited card also carries `turn` (its files and how to draw a step),
// `scope` ('turn' | 'session') and `focus` (the recorded path to open).
let view = null;

function showState(html) {
  els.changesState.innerHTML = html;
  els.changesState.hidden = false;
}

function hideState() {
  els.changesState.hidden = true;
  els.changesState.innerHTML = '';
}

function counts(add, del) {
  const parts = [];
  if (add != null) parts.push('<span class="chg-add">+' + add + '</span>');
  if (del != null) parts.push('<span class="chg-del">−' + del + '</span>');
  return parts.join(' ');
}

function note(text) {
  return '<div class="chg-note muted small">' + escapeHtml(text) + '</div>';
}

// The File view's words for a file too big to colour, above a diff left plain.
const LARGE_NOTE = '<div class="fv-note muted small">Colours are off for large files</div>';

// Once above the diffs when any of them skipped colour for size.
function noteLargeDiff(body) {
  if (body.querySelector('.tr-diff[data-tint="off"]')) body.insertAdjacentHTML('afterbegin', LARGE_NOTE);
}

// The session routes are terminal-grade (passkey); the project ones ride
// the bearer token like the rest of the Coding tab.
async function sessionApi(path, opts) {
  return terminalJsonApi('/api/claude-code/sessions/' + encodeURIComponent(view.sid) + path, opts);
}

// `diff --git` and `index` headers repeat what the row already says; every
// other line renders, tinted by its first character. Block-level spans so a
// wrapped long line keeps its tint across the continuation.
//
// `lang` (diffTint's answer, or null) picks token colours: the lines of each
// @@ hunk are tinted together, as in diff-view.js, with the prefix in a
// `.d-pfx` span. A tinted line carries no `.d-txt`, so Copy diff stays off for
// this panel as before.
export function renderDiff(text, lang) {
  const rows = [];
  const hunks = [];   // each @@ hunk's code rows, in order
  let hunk = null;   // null until a file's first @@ (the header lines before it are not code)
  String(text || '').split('\n').forEach(function (line, i, all) {
    if (i === all.length - 1 && line === '') return;   // trailing newline
    if (line.startsWith('diff --git')) hunk = null;
    if (line.startsWith('diff --git') || line.startsWith('index ')) return;
    let cls = 'd-ctx';
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'd-meta';
    else if (line.startsWith('@@')) cls = 'd-hunk';
    else if (line.startsWith('+')) cls = 'd-add';
    else if (line.startsWith('-')) cls = 'd-del';
    else if (line.startsWith('\\')) cls = 'd-meta';
    else if (/^(new file|deleted file|similarity|rename |old mode|new mode|Binary files)/.test(line)) cls = 'd-meta';
    const entry = { cls: cls, line: line, code: null };
    if (cls === 'd-hunk') {
      hunk = [];
      hunks.push(hunk);
    } else if (hunk && (cls === 'd-add' || cls === 'd-del' || cls === 'd-ctx')) {
      hunk.push(entry);
    }
    rows.push(entry);
  });
  hunks.forEach(function (h) {
    const code = tintDiffLines(h.map(function (r) { return r.line; }), lang);
    h.forEach(function (r, i) { r.code = code[i]; });
  });
  return rows.map(function (r) {
    if (r.code !== null) {
      return '<span class="' + r.cls + ' d-tint"><span class="d-pfx">' + escapeHtml(r.line.charAt(0)) +
        '</span>' + r.code + '</span>';
    }
    return '<span class="' + r.cls + '">' + (r.line === '' ? ' ' : escapeHtml(r.line)) + '</span>';
  }).join('');
}

async function loadProjectDiff(file, body) {
  const seq = view.seq;
  let res;
  try {
    res = await jsonApi(
      '/api/claude-code/changes/' + encodeURIComponent(view.id) +
        '/diff?path=' + encodeURIComponent(file.path)
    );
  } catch (exc) {
    if (!view || view.seq !== seq) return;
    body.innerHTML = note('Couldn’t read the diff');
    return;
  }
  if (!view || view.seq !== seq) return;
  if (res.binary) {
    body.innerHTML = note('Binary file');
    return;
  }
  if (!res.diff) {
    body.innerHTML = note('No textual changes');
    return;
  }
  const tint = diffTint(res.diff.split('\n'), file.path);
  let html = (tint.off ? LARGE_NOTE : '') + '<pre class="chg-diff">' + renderDiff(res.diff, tint.lang) + '</pre>';
  if (res.truncated) {
    html += note('Diff truncated at 200 KB — open in VS Code for the rest');
  }
  body.innerHTML = html;
}

function stepTime(ts) {
  const d = ts ? new Date(ts) : null;
  if (!d || isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// A session file's diff: each of its edits in order, under a quiet
// "Edit 2 of 3 · 10:42" line so a reader can tell where one ends.
async function loadSessionDiff(file, body) {
  const seq = view.seq;
  let res;
  try {
    res = await sessionApi('/changed-files/diff?path=' + encodeURIComponent(file.key));
  } catch (exc) {
    res = null;
  }
  if (!view || view.seq !== seq) return;
  if (!res || !res.available) {
    body.innerHTML = note('Couldn’t read this file’s edits');
    return;
  }
  const steps = res.steps || [];
  if (!steps.length) {
    body.innerHTML = note('No textual changes');
    return;
  }
  body.innerHTML = '';
  steps.forEach(function (step, i) {
    const label = document.createElement('div');
    label.className = 'chg-step muted small';
    const when = stepTime(step.timestamp);
    label.textContent = (step.deleted ? 'Deleted' : step.created ? 'Created' : 'Edit ' + (i + 1) + ' of ' + file.steps) +
      (when ? ' · ' + when : '');
    body.appendChild(label);
    if (!step.deleted) body.appendChild(renderHunks(step.diff, file.path));
  });
  noteLargeDiff(body);
  // Most agents record only an edit's own text, so their diffs have no line
  // numbers; say so once rather than leaving a missing gutter unexplained.
  if (steps.some(function (s) { return !s.deleted && s.diff && s.diff.hunks.length && !s.diff.numbered; })) {
    body.insertAdjacentHTML('beforeend', note('This agent’s diffs carry no line numbers'));
  }
  if (res.truncated) body.insertAdjacentHTML('beforeend', note('Diff truncated at 200 KB'));
}

// This turn's share of a file: each of the turn's own steps on it, in
// order, drawn from the entries the Chat page holds, so nothing is fetched.
function renderTurnDiff(file, body) {
  body.innerHTML = '';
  const n = file.edits.length;
  file.edits.forEach(function (e, i) {
    const a = e.action;
    const label = document.createElement('div');
    label.className = 'chg-step muted small';
    const when = stepTime(e.timestamp);
    label.textContent = (a.verb === 'deleted' ? 'Deleted' : a.created ? 'Created' : 'Edit ' + (i + 1) + ' of ' + n) +
      (when ? ' · ' + when : '');
    body.appendChild(label);
    if (a.verb === 'deleted') return;
    const diff = view.turn.renderStep(e);
    if (diff) body.appendChild(diff);
    else body.insertAdjacentHTML('beforeend', note('No textual changes'));
  });
  noteLargeDiff(body);
}

// --- a session file's tools: Diff | File, Copy path, Open on PC -----

// The path the session's routes know a file by: a turn row's recorded path,
// else the list's key (the recorded path, made absolute by the fold).
function requestPath(file) {
  return file.edits ? file.path : (file.key || file.path);
}

function tooLarge(size) {
  const kb = Math.ceil((size || 0) / 1024);
  return 'Too large to show here' + (kb ? ' (' + kb + ' KB)' : '') + '. The limit is 256 KB';
}

// The sentence for a file the File view can't show; Download toasts the same.
function fileReasonText(res) {
  const why = res && res.reason;
  return why === 'too_large' ? tooLarge(res.size)
    : FILE_REASON[why] || SESSION_REASON[why] || 'Couldn’t read this file';
}

// `read` is the file's memoised read (sessionFileBody), so the File pane,
// Download and Copy content share one request.
async function loadFileView(file, box, read) {
  const seq = view.seq;
  box.innerHTML = note('Loading…');
  const res = await read();
  if (!view || view.seq !== seq) return;
  if (!res || !res.available) {
    box.innerHTML = note(fileReasonText(res));
    return;
  }
  box.innerHTML = res.text ? renderFileView(file.path, res.text) : note('Empty file');
}

// iOS allows a clipboard write only inside the tap: writeText is called
// synchronously from the click, never after an await.
function copyText(text, done, failed) {
  try {
    navigator.clipboard.writeText(text).then(
      function () { toast(done, 'good', { icon: 'clipboard' }); },
      function (exc) { apiFailToast(failed, exc); }
    );
  } catch (exc) {
    apiFailToast(failed, exc);
  }
}

function copyPath(path) {
  copyText(path, 'Path copied', 'Couldn’t copy the path');
}

// What the Diff pane shows, as the patch text: every diff line's own text,
// prefix and hunk header included, without the gutter or an edit's step label.
// The unnumbered ⋯ between hunks of a gutterless diff is a separator, not a line.
function diffText(pane) {
  const lines = [];
  pane.querySelectorAll('.chg-diff > span').forEach(function (line) {
    if (line.classList.contains('d-hunk') && !line.querySelector('.d-ln')) return;
    const txt = line.querySelector('.d-txt');
    if (txt) lines.push(txt.textContent);
  });
  return lines.join('\n');
}

// A file saved under its own name. octet-stream, so Safari saves the blob
// instead of rendering a text blob inline.
function saveAs(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/octet-stream' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.hidden = true;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
}

async function openOnPc(file) {
  let res;
  try {
    res = await sessionApi('/changed-files/open', { method: 'POST', body: { path: requestPath(file) } });
  } catch (exc) {
    apiFailToast('Couldn’t open it on the PC', exc);
    return;
  }
  if (res && res.opened) {
    toast(res.with === 'explorer' ? 'Shown in Explorer' : 'Opened in VS Code', 'good', { icon: 'monitor' });
    return;
  }
  const why = res && res.reason;
  if (why) toast(FILE_REASON[why] || SESSION_REASON[why] || 'Couldn’t open it on the PC', 'error');
  else toast('Not opened: this is a test instance');
}

function toolButton(cls, glyph, label, onTap) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-button ' + cls;
  btn.setAttribute('aria-label', label);
  btn.title = label;
  btn.innerHTML = icon(glyph);
  if (onTap) btn.addEventListener('click', onTap);
  return btn;
}

// Download: the file's text under its own name. The route and its refusals
// are the File view's, so a file it can't show is refused with its sentence.
async function downloadFile(file, read) {
  const res = await read();
  if (!res || !res.available) {
    toast(fileReasonText(res), 'error');
    return;
  }
  saveAs(requestPath(file).split(/[\\/]/).pop() || 'file', res.text || '');
}

// The ⋮ menu of an opened file's tool row, one controller for the
// panel. It drops below the button group, right edges 4px in, as the Board
// drawer's kebab does; `changes-menu` lifts it over the panel (styles/shared.css).
const fileMenu = createRowMenu({
  className: 'changes-menu',
  placement: { against: function (kebab) { return kebab.parentElement; }, offset: 2, inset: 4 },
});

// An opened session file: the tool row, the file's absolute path as text you
// can select, then the Diff (the default) or the File, each loaded the first
// time it is shown and kept, so switching back costs nothing.
function sessionFileBody(file, body) {
  const full = windowsPath(requestPath(file), view.project);
  const tools = document.createElement('div');
  tools.className = 'chg-tools';
  const sw = document.createElement('div');
  sw.className = 'segmented chg-view';
  sw.setAttribute('role', 'group');
  sw.setAttribute('aria-label', 'Show the diff or the file');
  const panes = {};
  // Which pane shows, and the file's memoised read: one request serves the
  // File pane, Download and Copy content. A refusal isn't kept, so a retry
  // asks again.
  let active = 'diff';
  let pending = null;
  let fileText = null;
  function read() {
    if (!pending) {
      pending = sessionApi('/changed-files/content?path=' + encodeURIComponent(requestPath(file)))
        .catch(function () { return null; })
        .then(function (res) {
          if (res && res.available) fileText = res.text || '';
          else pending = null;
          return res;
        });
    }
    return pending;
  }
  [['diff', 'Diff'], ['file', 'File']].forEach(function (opt) {
    const seg = document.createElement('button');
    seg.type = 'button';
    seg.className = 'segmented-item';
    seg.dataset.view = opt[0];
    seg.setAttribute('aria-pressed', opt[0] === 'diff' ? 'true' : 'false');
    seg.textContent = opt[1];
    sw.appendChild(seg);
    const pane = document.createElement('div');
    pane.className = 'chg-pane chg-pane-' + opt[0];
    pane.hidden = opt[0] !== 'diff';
    panes[opt[0]] = pane;
  });
  sw.addEventListener('click', function (ev) {
    const seg = ev.target.closest('.segmented-item');
    if (!seg || !view) return;
    const which = seg.dataset.view;
    active = which;
    sw.querySelectorAll('.segmented-item').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b === seg));
    });
    Object.keys(panes).forEach(function (k) { panes[k].hidden = k !== which; });
    if (which === 'file' && !panes.file.dataset.loaded) {
      panes.file.dataset.loaded = '1';
      loadFileView(file, panes.file, read);
    }
  });
  tools.appendChild(sw);
  const acts = document.createElement('span');
  acts.className = 'icon-button-group chg-acts';
  acts.appendChild(toolButton('chg-download', 'download', 'Download', function () { downloadFile(file, read); }));
  const kebab = toolButton('chg-kebab', 'ellipsis-vertical', 'More actions', null);
  acts.appendChild(kebab);
  fileMenu.attach(requestPath(file), kebab, [
    {
      dataset: { item: 'chg-copy-content' }, glyph: 'clipboard', label: 'Copy content',
      hidden: function () { return active !== 'file' || !fileText; },
      onTap: function () { copyText(fileText, 'Content copied', 'Couldn’t copy the content'); },
    },
    {
      dataset: { item: 'chg-copy-diff' }, glyph: 'clipboard', label: 'Copy diff',
      hidden: function () { return active !== 'diff' || !diffText(panes.diff); },
      onTap: function () { copyText(diffText(panes.diff), 'Diff copied', 'Couldn’t copy the diff'); },
    },
    {
      dataset: { item: 'chg-copy-path' }, glyph: 'copy', label: 'Copy path',
      hidden: function () { return !full; },
      onTap: function () { copyPath(full); },
    },
    // Only where the PC is the screen in front of you: from the phone the
    // editor would open in front of nobody (the route refuses it too).
    {
      dataset: { item: 'chg-open-pc' }, glyph: 'monitor', label: 'Open on PC',
      hidden: function () { return !onThePc(); },
      onTap: function () { openOnPc(file); },
    },
  ]);
  tools.appendChild(acts);
  body.appendChild(tools);
  if (full) {
    const where = document.createElement('div');
    where.className = 'chg-fullpath';
    where.textContent = full;
    body.appendChild(where);
  }
  body.append(panes.diff, panes.file);
  if (file.edits) {
    renderTurnDiff(file, panes.diff);
    return;
  }
  panes.diff.innerHTML = note('Loading…');
  loadSessionDiff(file, panes.diff);
}

// "dir/name" → {dir, base}.
function splitPath(path) {
  const slash = path.lastIndexOf('/');
  return slash >= 0
    ? { dir: path.slice(0, slash), base: path.slice(slash + 1) }
    : { dir: '', base: path };
}

function fileRow(file) {
  const det = document.createElement('details');
  det.className = 'chg-file';
  det.dataset.path = file.path;
  const sum = document.createElement('summary');
  sum.appendChild(fileBadge(file.status));
  const parts = file.base != null ? file : splitPath(file.path);
  sum.appendChild(fileName(parts.base, parts.dir, file.old_path ? 'renamed from ' + file.old_path : '', file.path));
  if (file.staged) {
    const pip = document.createElement('span');
    pip.className = 'chg-staged';
    pip.title = 'staged';
    pip.setAttribute('aria-label', 'staged');
    sum.appendChild(pip);
  }
  sum.appendChild(fileCounts(file.additions, file.deletions, file.binary));
  det.appendChild(sum);
  const body = document.createElement('div');
  body.className = 'chg-body';
  det.appendChild(body);
  let loaded = false;
  det.addEventListener('toggle', function () {
    if (det.open && !loaded && view) {
      loaded = true;
      if (view.kind === 'session') {
        sessionFileBody(file, body);
        return;
      }
      body.innerHTML = note('Loading…');
      loadProjectDiff(file, body);
    }
  });
  return det;
}

function group(title, files) {
  const wrap = document.createElement('section');
  wrap.className = 'chg-group';
  const h = document.createElement('h3');
  h.className = 'chg-group-title';
  h.textContent = title + ' (' + files.length + ')';
  wrap.appendChild(h);
  files.forEach(function (f) { wrap.appendChild(fileRow(f)); });
  return wrap;
}

function summaryLine(files, c, tail) {
  const summary = document.createElement('div');
  summary.className = 'chg-summary muted small';
  summary.innerHTML = files.length + (files.length === 1 ? ' file' : ' files') +
    ' · ' + counts(c.additions || 0, c.deletions || 0) + (tail ? ' · ' + escapeHtml(tail) : '');
  return summary;
}

function renderProject(body) {
  const list = els.changesList;
  list.innerHTML = '';
  els.changesTitle.innerHTML = escapeHtml(view.name) +
    (body.branch ? '<span class="chg-branch">' + icon('git-branch') + escapeHtml(body.branch) + '</span>' : '');
  const files = body.files || [];
  if (!files.length) {
    showState(icon('git-branch') + '<div>Working tree clean</div>');
    return;
  }
  hideState();
  list.appendChild(summaryLine(files, body.counts || {}));
  const tracked = files.filter(function (f) { return f.status !== 'U'; });
  const untracked = files.filter(function (f) { return f.status === 'U'; });
  if (tracked.length) list.appendChild(group('Changes', tracked));
  if (untracked.length) list.appendChild(group('Untracked', untracked));
}

// "This turn · Whole session", only on a panel opened from a turn's
// Edited card. Switching keeps the focused file and re-renders the list.
function scopeSwitch() {
  const wrap = document.createElement('div');
  wrap.className = 'chg-scope';
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', 'Which changes');
  [['turn', 'This turn'], ['session', 'Whole session']].forEach(function (opt) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chg-scope-btn';
    btn.dataset.scope = opt[0];
    btn.setAttribute('aria-pressed', view.scope === opt[0] ? 'true' : 'false');
    const pill = document.createElement('span');
    pill.textContent = opt[1];
    btn.appendChild(pill);
    btn.addEventListener('click', function () {
      if (!view || view.scope === opt[0]) return;
      view.scope = opt[0];
      load();
    });
    wrap.appendChild(btn);
  });
  return wrap;
}

// Does a session row stand for the file a card row named? Claude records an
// absolute path, which is the row's key; another agent's may be relative to
// its folder, which the row's shown path or the end of its key then matches.
function sameFile(row, path) {
  const k = fileKey(path);
  const key = fileKey(row.key || row.path);
  return key === k || fileKey(row.path) === k || key.endsWith('/' + k);
}

// Open the focused file's row and bring it to the top of the panel. Opening
// it loads its diff the way a tap does.
function showFocus(files, rows) {
  if (!view.focus) return;
  const i = files.findIndex(function (f) { return sameFile(f, view.focus); });
  if (i < 0) return;
  const det = rows[i];
  det.classList.add('chg-focus');
  det.open = true;
  const box = els.changesList.parentElement;
  if (box) box.scrollTop += det.getBoundingClientRect().top - box.getBoundingClientRect().top - 12;
}

function fileList(files) {
  const wrap = document.createElement('section');
  wrap.className = 'chg-group';
  const rows = files.map(fileRow);
  rows.forEach(function (r) { wrap.appendChild(r); });
  return { wrap: wrap, rows: rows };
}

// The turn's files, from the card that opened the panel: sorted like the
// session's list, with the turn's own counts.
function renderTurn() {
  const list = els.changesList;
  list.innerHTML = '';
  hideState();
  const files = view.turn.files.map(function (f) {
    const shown = f.dir ? f.dir + '/' + f.base : f.base;
    return Object.assign({}, f, { shown: shown });
  }).sort(function (x, y) { return x.shown.toLowerCase().localeCompare(y.shown.toLowerCase()); });
  const c = files.reduce(function (s, f) {
    return { additions: s.additions + f.additions, deletions: s.deletions + f.deletions };
  }, { additions: 0, deletions: 0 });
  list.appendChild(summaryLine(files, c, 'this turn'));
  list.appendChild(scopeSwitch());
  if (view.turn.partial) {
    list.insertAdjacentHTML('beforeend', note('Earlier steps of this turn aren’t loaded in Chat'));
  }
  const built = fileList(files);
  list.appendChild(built.wrap);
  showFocus(files, built.rows);
}

// One flat list: a session's files are all "what this session changed", so
// there is nothing to group, and the summary names the source it read.
function renderSession(body) {
  const list = els.changesList;
  list.innerHTML = '';
  const files = body.files || [];
  if (!files.length) {
    showState(icon('file-diff') + '<div>No files changed in this session</div>');
    if (view.turn) list.appendChild(scopeSwitch());
    return;
  }
  hideState();
  list.appendChild(summaryLine(files, body.counts || {}, 'from this session’s transcript'));
  if (view.turn) list.appendChild(scopeSwitch());
  if (body.partial) {
    list.insertAdjacentHTML('beforeend', note('A very long transcript: only its newest part was read'));
  }
  if (!body.project_exists) {
    list.insertAdjacentHTML('beforeend',
      note('The project folder is gone, so deleted files can’t be told apart'));
  }
  const built = fileList(files);
  list.appendChild(built.wrap);
  showFocus(files, built.rows);
}

async function loadProject(seq) {
  showState(icon('git-branch') + '<div>Reading working tree…</div>');
  let body;
  try {
    body = await jsonApi('/api/claude-code/changes/' + encodeURIComponent(view.id));
  } catch (exc) {
    if (!view || view.seq !== seq) return;
    const status = exc && exc.status;
    showState(icon('git-branch') + '<div>' +
      (status === 409 ? 'Not a git repository' : 'Couldn’t read the working tree') + '</div>');
    return;
  }
  if (!view || view.seq !== seq) return;
  renderProject(body);
}

async function loadSession(seq) {
  showState(icon('file-diff') + '<div>Reading the transcript…</div>');
  let body;
  try {
    body = await sessionApi('/changed-files');
  } catch (exc) {
    body = null;
  }
  if (!view || view.seq !== seq) return;
  if (!body || !body.available) {
    const why = (body && SESSION_REASON[body.reason]) || 'Couldn’t read the transcript';
    showState(icon('file-diff') + '<div>' + escapeHtml(why) + '</div>');
    return;
  }
  renderSession(body);
}

function load() {
  if (!view) return;
  const seq = ++view.seq;
  fileMenu.close();   // the rows are rebuilt below
  els.changesList.innerHTML = '';
  if (view.kind === 'session' && view.scope === 'turn') renderTurn();
  else if (view.kind === 'session') loadSession(seq);
  else loadProject(seq);
}

function open(next, refreshLabel) {
  if (!els.changesOverlay) return;
  view = next;
  els.changesTitle.textContent = next.name;
  els.changesRefresh.title = refreshLabel;
  els.changesOverlay.hidden = false;
  load();
}

// `a` is a Coding-tab app entry ({id, name}).
export function openChanges(a) {
  open({ kind: 'project', id: a.id, name: a.name, seq: 0 }, 'Re-read the working tree');
}

// `s` is a session as the list knows it ({session_id, name, project_dir, …}); `title`
// is what the session bar shows for it. `opts` (an Edited card's tap):
// `turn` — {files, partial, renderStep(entry)} — starts the panel on that
// turn with the scope pair; `focus` is the recorded path to open.
export function openSessionChanges(s, title, opts) {
  const o = opts || {};
  open(
    {
      kind: 'session', sid: s.session_id, name: 'Changed files · ' + (title || s.name || 'session'), seq: 0,
      project: s.project_dir || null,
      turn: o.turn || null, scope: o.turn ? 'turn' : 'session', focus: o.focus || null,
    },
    'Re-read the transcript'
  );
}

export function closeChanges() {
  if (view) view.seq += 1;   // any in-flight response lands nowhere
  view = null;
  fileMenu.close();
  if (!els.changesOverlay) return;
  els.changesOverlay.hidden = true;
  els.changesList.innerHTML = '';
  hideState();
}

export function wireChanges() {
  if (!els.changesOverlay) return;
  els.changesClose.addEventListener('click', closeChanges);
  els.changesRefresh.addEventListener('click', function () { load(); });
  document.addEventListener('keydown', function (ev) {
    if (view && ev.key === 'Escape') closeChanges();
  });
}
