/* "Edited N files" card (#1477, Step 4/6 of #1472) and the file row it
 * shares with the Changed files panel.
 *
 * When an agent's turn is over, a card closes it: a header with the turn's
 * total +N −M, then one row per file the turn edited (an A/M/D badge, the
 * file's name in mono with its folder as the hint, its own +N −M), the first
 * SHOW_FIRST of them and then "Show N more". A row opens the Changed files
 * panel focused on that file; the header opens it on the turn as a whole.
 *
 * Folded on the client from the steps the page already holds — each edit
 * call's server-side `action` — never a new request, so the card's counts are
 * the step lines' counts by construction. A turn whose start is on a page not
 * loaded yet counts what is loaded and says so, with Load older beside it.
 *
 * Only the Chat pane mounts the card: the Life OS conversation viewer shares
 * the renderer but has no session to open a panel against. This module draws
 * and folds; session-transcript.js decides when a turn is over and what a tap
 * opens.
 */

import { icon } from './_vendored/icons/icons.js';

// Long names for the one-letter status badges (VS Code's vocabulary).
export const STATUS_NAME = {
  M: 'modified', A: 'added', D: 'deleted', R: 'renamed',
  T: 'type changed', U: 'untracked', C: 'conflict',
};

// Rows shown before "Show N more", on every width (decision 8 of #1472).
export const SHOW_FIRST = 3;

const EDIT_VERBS = ['edited', 'wrote', 'deleted'];

// One file's identity across a turn's steps: Windows paths compare
// case-insensitively and with either slash, as the server's fold does.
export function fileKey(path) {
  return String(path || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

// Every file `entries` edited, in the order the turn first touched them:
// `{path, key, status, additions, deletions, edits}`. The status follows the
// server's Changed files fold: A when the first touch created the file, D
// once a step deletes it, M again if a later step edits it back. A failed
// call changed nothing, so it never counts — the step line's rule.
export function foldEdits(entries) {
  const files = new Map();
  entries.forEach(function (e) {
    const a = e.kind === 'tool_call' ? e.action : null;
    if (!a || EDIT_VERBS.indexOf(a.verb) === -1 || !a.path || e.error === true) return;
    const key = fileKey(a.path);
    let row = files.get(key);
    if (!row) {
      row = { path: a.path, key: key, status: a.created ? 'A' : 'M', additions: 0, deletions: 0, edits: [] };
      files.set(key, row);
    }
    if (a.verb === 'deleted') row.status = 'D';
    else if (row.status === 'D') row.status = 'M';
    row.additions += a.added || 0;
    row.deletions += a.removed || 0;
    row.edits.push(e);
  });
  return Array.from(files.values());
}

// --- the shared file row ------------------------------------------------------
//
// Badge, name over folder, counts: the card's rows and the panel's are the
// same three parts, so a file reads the same in both places.

export function fileBadge(status) {
  const badge = document.createElement('span');
  badge.className = 'chg-badge chg-badge-' + status;
  badge.textContent = status;
  badge.title = STATUS_NAME[status] || status;
  return badge;
}

// Name first, the folder under it as the hint ("project root" for a file at
// the top). `note` trails the folder (a rename's old path). The whole path
// stays on `title`.
export function fileName(base, dir, note, title) {
  const name = document.createElement('span');
  name.className = 'chg-path';
  if (title) name.title = title;
  const b = document.createElement('span');
  b.className = 'chg-base';
  b.textContent = base;
  const d = document.createElement('span');
  d.className = 'chg-dir';
  d.textContent = (dir || 'project root') + (note ? ' · ' + note : '');
  name.append(b, d);
  return name;
}

export function fileCounts(additions, deletions, binary) {
  const n = document.createElement('span');
  n.className = 'chg-counts';
  if (binary) {
    const bin = document.createElement('span');
    bin.className = 'muted';
    bin.textContent = 'bin';
    n.appendChild(bin);
    return n;
  }
  const add = document.createElement('span');
  add.className = 'chg-add';
  add.textContent = '+' + (additions || 0);
  const del = document.createElement('span');
  del.className = 'chg-del';
  del.textContent = '−' + (deletions || 0);
  n.append(add, ' ', del);
  return n;
}

// --- the card -----------------------------------------------------------------

function chevron() {
  const span = document.createElement('span');
  span.className = 'tr-edited-chev';
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = icon('chevron-right');
  return span;
}

// `files` are foldEdits() rows, each with `base` and `dir` set by the caller
// (relative to the session's project). `opts`: `expanded` (Show N more was
// tapped), `partial` (the turn's start is not loaded), and the taps —
// `onOpen(file)` (null for the header), `onMore()`, `onLoadOlder()`.
export function renderEditedCard(files, opts) {
  const card = document.createElement('section');
  card.className = 'tr-edited';
  card.setAttribute('aria-label', 'Files this turn edited');
  const total = files.reduce(function (s, f) {
    return { a: s.a + f.additions, d: s.d + f.deletions };
  }, { a: 0, d: 0 });

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'tr-edited-head';
  head.setAttribute('aria-label', 'Open Changed files for this turn');
  head.insertAdjacentHTML('beforeend', icon('file-diff'));
  const title = document.createElement('span');
  title.className = 'tr-edited-title';
  title.textContent = 'Edited ' + files.length + (files.length === 1 ? ' file' : ' files');
  head.append(title, fileCounts(total.a, total.d), chevron());
  head.addEventListener('click', function () { opts.onOpen(null); });
  card.appendChild(head);

  const shown = opts.expanded ? files : files.slice(0, SHOW_FIRST);
  shown.forEach(function (f) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'tr-edited-row';
    row.dataset.path = f.path;
    row.append(
      fileBadge(f.status),
      fileName(f.base, f.dir, '', f.path),
      fileCounts(f.additions, f.deletions),
      chevron()
    );
    row.addEventListener('click', function () { opts.onOpen(f); });
    card.appendChild(row);
  });

  const rest = files.length - shown.length;
  if (rest > 0) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'tr-edited-more';
    more.textContent = 'Show ' + rest + ' more';
    more.addEventListener('click', function () { opts.onMore(); });
    card.appendChild(more);
  }

  if (opts.partial) {
    const note = document.createElement('div');
    note.className = 'tr-edited-partial';
    note.append('Earlier steps not loaded · ');
    const older = document.createElement('button');
    older.type = 'button';
    older.className = 'tr-edited-older';
    older.textContent = 'Load older';
    older.addEventListener('click', function () { opts.onLoadOlder(); });
    note.appendChild(older);
    card.appendChild(note);
  }
  return card;
}
