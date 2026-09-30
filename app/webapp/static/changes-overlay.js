/* "Show changes" overlay (#977): a read-only, phone-first view of one
 * project's working tree — what a red (dirty) Coding tile actually means,
 * without opening VS Code just to look. Since #1349 the same panel also
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
 */

import { els } from './state.js';
import { authHeaders, escapeHtml, jsonApi } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { renderHunks } from './diff-view.js';
import { ensureTerminalToken } from './webauthn.js';

// Long names for the one-letter status badges (VS Code's vocabulary).
const STATUS_NAME = {
  M: 'modified', A: 'added', D: 'deleted', R: 'renamed',
  T: 'type changed', U: 'untracked', C: 'conflict',
};

// Why a session's Changed files could not be read, in the panel's words.
const SESSION_REASON = {
  no_transcript: 'No transcript for this session',
  unsupported_agent: 'Changed files is not available for this agent',
  session_not_found: 'This session has ended',
  session_host_unreachable: 'The session host is unreachable',
};

// null while closed, else what is shown: a project ({kind: 'project', id,
// name}) or a session ({kind: 'session', sid, name}). `seq` guards a slow
// response from an earlier open landing in a newer view.
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

// The session routes are terminal-grade (passkey); the project ones ride
// the bearer token like the rest of the Coding tab.
async function sessionApi(path) {
  const tt = await ensureTerminalToken();
  return jsonApi(
    '/api/claude-code/sessions/' + encodeURIComponent(view.sid) + path,
    { headers: authHeaders({ terminalToken: tt }) }
  );
}

// `diff --git` and `index` headers repeat what the row already says; every
// other line renders, tinted by its first character. Block-level spans so a
// wrapped long line keeps its tint across the continuation.
export function renderDiff(text) {
  const out = [];
  String(text || '').split('\n').forEach(function (line, i, all) {
    if (i === all.length - 1 && line === '') return;   // trailing newline
    if (line.startsWith('diff --git') || line.startsWith('index ')) return;
    let cls = 'd-ctx';
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'd-meta';
    else if (line.startsWith('@@')) cls = 'd-hunk';
    else if (line.startsWith('+')) cls = 'd-add';
    else if (line.startsWith('-')) cls = 'd-del';
    else if (line.startsWith('\\')) cls = 'd-meta';
    else if (/^(new file|deleted file|similarity|rename |old mode|new mode|Binary files)/.test(line)) cls = 'd-meta';
    out.push('<span class="' + cls + '">' + (line === '' ? ' ' : escapeHtml(line)) + '</span>');
  });
  return out.join('');
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
  let html = '<pre class="chg-diff">' + renderDiff(res.diff) + '</pre>';
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
    if (!step.deleted) body.appendChild(renderHunks(step.diff));
  });
  // Most agents record only an edit's own text, so their diffs have no line
  // numbers (#1356); say so once rather than leaving a missing gutter unexplained.
  if (steps.some(function (s) { return !s.deleted && s.diff && s.diff.hunks.length && !s.diff.numbered; })) {
    body.insertAdjacentHTML('beforeend', note('This agent’s diffs carry no line numbers'));
  }
  if (res.truncated) body.insertAdjacentHTML('beforeend', note('Diff truncated at 200 KB'));
}

function fileRow(file) {
  const det = document.createElement('details');
  det.className = 'chg-file';
  det.dataset.path = file.path;
  const sum = document.createElement('summary');
  const badge = document.createElement('span');
  badge.className = 'chg-badge chg-badge-' + file.status;
  badge.textContent = file.status;
  badge.title = STATUS_NAME[file.status] || file.status;
  sum.appendChild(badge);
  const path = document.createElement('span');
  path.className = 'chg-path';
  const slash = file.path.lastIndexOf('/');
  const dir = slash >= 0 ? file.path.slice(0, slash + 1) : '';
  const base = slash >= 0 ? file.path.slice(slash + 1) : file.path;
  path.innerHTML = (dir ? '<span class="chg-dir">' + escapeHtml(dir) + '</span>' : '') +
    '<span class="chg-base">' + escapeHtml(base) + '</span>' +
    (file.old_path ? '<span class="chg-dir"> renamed from ' + escapeHtml(file.old_path) + '</span>' : '');
  sum.appendChild(path);
  if (file.staged) {
    const pip = document.createElement('span');
    pip.className = 'chg-staged';
    pip.title = 'staged';
    pip.setAttribute('aria-label', 'staged');
    sum.appendChild(pip);
  }
  const n = document.createElement('span');
  n.className = 'chg-counts';
  n.innerHTML = file.binary ? '<span class="muted">bin</span>' : counts(file.additions, file.deletions);
  sum.appendChild(n);
  det.appendChild(sum);
  const body = document.createElement('div');
  body.className = 'chg-body';
  det.appendChild(body);
  let loaded = false;
  det.addEventListener('toggle', function () {
    if (det.open && !loaded && view) {
      loaded = true;
      body.innerHTML = note('Loading…');
      (view.kind === 'session' ? loadSessionDiff : loadProjectDiff)(file, body);
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

// One flat list: a session's files are all "what this session changed", so
// there is nothing to group, and the summary names the source it read.
function renderSession(body) {
  const list = els.changesList;
  list.innerHTML = '';
  const files = body.files || [];
  if (!files.length) {
    showState(icon('file-diff') + '<div>No files changed in this session</div>');
    return;
  }
  hideState();
  list.appendChild(summaryLine(files, body.counts || {}, 'from this session’s transcript'));
  if (body.partial) {
    list.insertAdjacentHTML('beforeend', note('A very long transcript: only its newest part was read'));
  }
  if (!body.project_exists) {
    list.insertAdjacentHTML('beforeend',
      note('The project folder is gone, so deleted files can’t be told apart'));
  }
  const wrap = document.createElement('section');
  wrap.className = 'chg-group';
  files.forEach(function (f) { wrap.appendChild(fileRow(f)); });
  list.appendChild(wrap);
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
  els.changesList.innerHTML = '';
  if (view.kind === 'session') loadSession(seq);
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

// `s` is a session as the list knows it ({session_id, name, …}); `title`
// is what the session bar shows for it.
export function openSessionChanges(s, title) {
  open(
    { kind: 'session', sid: s.session_id, name: 'Changed files · ' + (title || s.name || 'session'), seq: 0 },
    'Re-read the transcript'
  );
}

export function closeChanges() {
  if (view) view.seq += 1;   // any in-flight response lands nowhere
  view = null;
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
