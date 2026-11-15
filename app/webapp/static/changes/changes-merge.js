/* A long session's Changed files, read one transcript chunk at a time: the
 * "Load older" line and the merge of an older chunk into the rows already
 * loaded. Pure: no panel state, no requests (changes-overlay.js owns those).
 */

// "A very long transcript: only part of it was read. [Load older]". The older
// part comes one chunk per tap, merged into the list; the line goes once the
// start of the transcript is read.
export function olderLine(loading, onLoad) {
  const line = document.createElement('div');
  line.className = 'chg-note chg-partial muted small';
  line.append('A very long transcript: only part of it was read. ');
  const older = document.createElement('button');
  older.dataset.track = 'changes.load-older';
  older.type = 'button';
  older.className = 'chg-older';
  older.textContent = loading ? 'Loading…' : 'Load older';
  older.disabled = !!loading;
  older.addEventListener('click', onLoad);
  line.appendChild(older);
  return line;
}

// A file's row keyed like the server folds it: either slash, any case.
function rowKey(row) {
  return String(row.key || row.path).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

// Fold one older chunk's rows into `state.files`. A file seen in both is one
// row with the summed counts, its path spelled as its first (older) edit gave
// it; it is deleted if the newer part says so, else created if the older part
// created it. The caller re-totals `state.counts`.
export function mergeOlder(state, body) {
  const byKey = new Map(state.files.map(function (f) { return [rowKey(f), f]; }));
  (body.files || []).forEach(function (old) {
    const row = byKey.get(rowKey(old));
    if (!row) {
      const fresh = Object.assign({}, old);
      byKey.set(rowKey(fresh), fresh);
      state.files.push(fresh);
      return;
    }
    row.path = old.path;
    row.key = old.key;
    row.additions += old.additions;
    row.deletions += old.deletions;
    row.steps += old.steps;
    if (row.status === 'M' && old.status === 'A') row.status = 'A';
  });
  state.files.sort(function (x, y) {
    const a = String(x.path).toLowerCase();
    const b = String(y.path).toLowerCase();
    return a < b ? -1 : a > b ? 1 : 0;
  });
  state.next_cursor = body.next_cursor == null ? null : body.next_cursor;
  state.partial = state.next_cursor != null;
}
