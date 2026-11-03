// Unit pin for the Links sheet's "Read more" merge: an older window's links
// join the rows already shown, deduped by URL, without reordering them.
// Runs under plain Node; invoked by tests/unit/static_ui/test_links_merge.py.

import assert from 'node:assert/strict';

const { mergeLinks } = await import('../../app/webapp/static/links/links-merge.js');

const link = (url, over) => Object.assign({
  url, label: url, title: null, first_seen: 't1', last_seen: 't1', count: 1,
}, over);
const group = (key, links) => ({ key, title: key, total: links.length, links });
const resp = (groups, over) => Object.assign({
  groups, total: groups.reduce((n, g) => n + g.total, 0), partial: true, offset: 900, read_from: 'new',
}, over);

const base = resp([
  group('artifacts', [link('a1', { label: 'Artifact a1', last_seen: 't9', first_seen: 't8', count: 2 })]),
  group('other', [link('o1')]),
]);
const older = resp([
  group('artifacts', [
    link('a1', { label: 'Early Board', title: 'Early Board', first_seen: 't2', last_seen: 't3', count: 3 }),
    link('a2'),
  ]),
  group('github', [link('g1')]),
], { partial: false, offset: 0, read_from: 'old' });

const merged = mergeLinks(base, older);

// New URLs follow the rows already shown; the groups keep their fixed order.
assert.deepEqual(merged.groups.map((g) => g.key), ['artifacts', 'github', 'other']);
assert.deepEqual(merged.groups[0].links.map((l) => l.url), ['a1', 'a2']);

// A URL in both windows is one row: newer last mention, older first, summed
// count, and the name the older window knew.
const a1 = merged.groups[0].links[0];
assert.deepEqual([a1.last_seen, a1.first_seen, a1.count], ['t9', 't2', 5]);
assert.deepEqual([a1.label, a1.title], ['Early Board', 'Early Board']);

// Counts are of distinct URLs; the cursor moves to the older window.
assert.equal(merged.groups[0].total, 2);
assert.equal(merged.total, 4);
assert.deepEqual([merged.partial, merged.offset, merged.read_from], [false, 0, 'old']);

// A title the newer window already had is kept.
const kept = mergeLinks(
  resp([group('artifacts', [link('a1', { title: 'Mine', label: 'Mine' })])]),
  resp([group('artifacts', [link('a1', { title: 'Theirs', label: 'Theirs' })])]),
);
assert.equal(kept.groups[0].links[0].title, 'Mine');

// The inputs are not changed.
assert.equal(base.groups[0].links[0].count, 2);
assert.equal(base.groups.length, 2);

console.log('OK');
