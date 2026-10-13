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

// { input, list, empty, storageKey, onApply } -> { apply, set }
// onApply(query), optional: told the folded query after every apply, so a
// list can react to filtering as a whole (Life's fold opens, #1503).
// set(value) replaces the text as typing would: through an input event, so
// any other listener on the field (Life's conversation search) hears it too.
export function listFilter(opts) {
  const input = opts.input;
  if (!input) return { apply: function () {}, set: function () {} };
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
    if (opts.onApply) opts.onApply(query);
  }

  function set(value) {
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  input.addEventListener('input', function () {
    save(opts.storageKey, input.value.trim());
    apply();
  });
  // The no-match empty state's one action (#1238 J-09): clear the filter.
  const clear = opts.empty && opts.empty.querySelector('.empty-state-action');
  if (clear) {
    clear.addEventListener('click', function () {
      set('');
      input.focus();
    });
  }
  return { apply: apply, set: set };
}
