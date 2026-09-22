/* "Show changes" overlay (#977): a read-only, phone-first view of one
 * project's working tree — what a red (dirty) Coding tile actually means,
 * without opening VS Code just to look.
 *
 * Shape borrowed from GitHub's "Files changed": files grouped into Changes
 * and Untracked, each a collapsible row (status badge · path · +N −N) whose
 * unified diff loads lazily on first open from
 * `/api/claude-code/changes/{id}/diff?path=`. No library: the diff is
 * tinted per line (`+` / `-` / `@@`) by a tiny renderer below. A viewer
 * only — nothing here stages, commits or discards.
 */

import { els } from './state.js';
import { escapeHtml, jsonApi } from './api.js';
import { icon } from './_vendored/icons/icons.js';

// Long names for the one-letter status badges (VS Code's vocabulary).
const STATUS_NAME = {
  M: 'modified', A: 'added', D: 'deleted', R: 'renamed',
  T: 'type changed', U: 'untracked', C: 'conflict',
};

// null while closed, else the project shown. `seq` guards a slow response
// from an earlier open landing in a newer view.
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

async function loadDiff(file, body) {
  if (!view) return;
  const seq = view.seq;
  body.innerHTML = '<div class="chg-note muted small">Loading…</div>';
  let res;
  try {
    res = await jsonApi(
      '/api/claude-code/changes/' + encodeURIComponent(view.projectId) +
        '/diff?path=' + encodeURIComponent(file.path)
    );
  } catch (exc) {
    if (!view || view.seq !== seq) return;
    body.innerHTML = '<div class="chg-note muted small">Couldn’t read the diff</div>';
    return;
  }
  if (!view || view.seq !== seq) return;
  if (res.binary) {
    body.innerHTML = '<div class="chg-note muted small">Binary file</div>';
    return;
  }
  if (!res.diff) {
    body.innerHTML = '<div class="chg-note muted small">No textual changes</div>';
    return;
  }
  let html = '<pre class="chg-diff">' + renderDiff(res.diff) + '</pre>';
  if (res.truncated) {
    html += '<div class="chg-note muted small">Diff truncated at 200 KB — open in VS Code for the rest</div>';
  }
  body.innerHTML = html;
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
    if (det.open && !loaded) {
      loaded = true;
      loadDiff(file, body);
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

function render(body) {
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
  const summary = document.createElement('div');
  summary.className = 'chg-summary muted small';
  const c = body.counts || {};
  summary.innerHTML = files.length + (files.length === 1 ? ' file' : ' files') +
    ' · ' + counts(c.additions || 0, c.deletions || 0);
  list.appendChild(summary);
  const tracked = files.filter(function (f) { return f.status !== 'U'; });
  const untracked = files.filter(function (f) { return f.status === 'U'; });
  if (tracked.length) list.appendChild(group('Changes', tracked));
  if (untracked.length) list.appendChild(group('Untracked', untracked));
}

async function load() {
  if (!view) return;
  const seq = ++view.seq;
  els.changesList.innerHTML = '';
  showState(icon('git-branch') + '<div>Reading working tree…</div>');
  let body;
  try {
    body = await jsonApi('/api/claude-code/changes/' + encodeURIComponent(view.projectId));
  } catch (exc) {
    if (!view || view.seq !== seq) return;
    const status = exc && exc.status;
    showState(icon('git-branch') + '<div>' +
      (status === 409 ? 'Not a git repository' : 'Couldn’t read the working tree') + '</div>');
    return;
  }
  if (!view || view.seq !== seq) return;
  render(body);
}

// `a` is a Coding-tab app entry ({id, name}).
export function openChanges(a) {
  if (!els.changesOverlay) return;
  view = { projectId: a.id, name: a.name, seq: 0 };
  els.changesTitle.textContent = a.name;
  els.changesOverlay.hidden = false;
  load();
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
