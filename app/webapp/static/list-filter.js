/* A name filter above a long list (#1132, the action-row contract's filter
 * field — design.md: a list that can pass ~12 rows gets one).
 *
 * Client-side substring match on each row's `.action-row-title`, case
 * folded. The text persists per list in localStorage, so a filtered list
 * survives a reload. Rows re-render on polls, so the list's renderer calls
 * `apply()` after every rebuild. With a query and no match, the list's
 * canonical empty-state block shows instead.
 */

function load(key) {
  try { return localStorage.getItem(key) || ''; } catch (exc) { return ''; }
}

function save(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch (exc) { /* private mode: the filter just doesn't persist */ }
}

// { input, list, empty, storageKey } -> { apply }
export function listFilter(opts) {
  const input = opts.input;
  if (!input) return { apply: function () {} };
  input.value = load(opts.storageKey);

  function apply() {
    const query = input.value.trim().toLowerCase();
    let rows = 0;
    let shown = 0;
    opts.list.querySelectorAll(':scope > li').forEach(function (li) {
      const title = li.querySelector('.action-row-title');
      if (!title) return;  // a note row (the favorites-empty line), not a row
      rows += 1;
      const hit = !query || title.textContent.toLowerCase().indexOf(query) !== -1;
      li.hidden = !hit;
      if (hit) shown += 1;
    });
    if (opts.empty) opts.empty.hidden = !(query && rows && !shown);
  }

  input.addEventListener('input', function () {
    save(opts.storageKey, input.value.trim());
    apply();
  });
  return { apply: apply };
}
