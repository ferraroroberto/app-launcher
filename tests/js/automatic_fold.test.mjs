// Pin for the Chat pane's fold of automatic turns: which list items form a
// group, what ends one, and the per-session default. The grouping is a pure
// function of item kinds, so it runs here under plain Node; the divider's
// markup and taps are the browser test's.
// Invoked by tests/unit/static_ui/test_automatic_fold.py.

import assert from 'node:assert/strict';

const { foldGroups, automaticDefault, rememberAutomatic } =
  await import('../../app/webapp/static/chat/automatic-fold.js');

const AUTO = { kind: 'auto' };
const HUMAN = { kind: 'human' };
const agent = (keep) => ({ kind: 'agent', keep: !!keep });

// One automatic turn: a group of one turn covering the prompt and the reply.
assert.deepEqual(foldGroups([HUMAN, agent(), AUTO, agent(), HUMAN, agent()]),
  [{ start: 2, end: 3, turns: 1 }]);

// Consecutive automatic turns merge into one group.
assert.deepEqual(foldGroups([AUTO, agent(), AUTO, agent(), AUTO, agent()]),
  [{ start: 0, end: 5, turns: 3 }]);

// An automatic prompt with no reply yet still folds, and merges with its neighbour.
assert.deepEqual(foldGroups([AUTO, agent(), AUTO]), [{ start: 0, end: 2, turns: 2 }]);

// A turn that holds a question, a plan or a sent file never folds and splits the run.
assert.deepEqual(foldGroups([AUTO, agent(), AUTO, agent(true), AUTO, agent()]),
  [{ start: 0, end: 1, turns: 1 }, { start: 4, end: 5, turns: 1 }]);
assert.deepEqual(foldGroups([AUTO, agent(true)]), []);

// A human prompt between two automatic runs keeps them apart.
assert.deepEqual(foldGroups([AUTO, agent(), HUMAN, agent(), AUTO, agent()]),
  [{ start: 0, end: 1, turns: 1 }, { start: 4, end: 5, turns: 1 }]);

// A reply with no automatic prompt before it (a human's, or one cut by a page
// boundary) is never part of a group, and neither is any other list item.
assert.deepEqual(foldGroups([agent(), HUMAN, agent()]), []);
assert.deepEqual(foldGroups([agent(), AUTO, agent()]), [{ start: 1, end: 2, turns: 1 }]);
assert.deepEqual(foldGroups([AUTO, { kind: 'other' }, AUTO, agent()]),
  [{ start: 0, end: 0, turns: 1 }, { start: 2, end: 3, turns: 1 }]);
assert.deepEqual(foldGroups([]), []);

// The per-session default: on for the chief, off elsewhere, a saved choice wins.
const store = new Map();
globalThis.window = {
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
  },
};
assert.equal(automaticDefault('s1', true), true);
assert.equal(automaticDefault('s1', false), false);
rememberAutomatic('s1', false);
assert.equal(automaticDefault('s1', true), false);
rememberAutomatic('s2', true);
assert.equal(automaticDefault('s2', false), true);
assert.equal(automaticDefault('s3', true), true);  // another session is unaffected

// A browser that throws on storage falls back to the default instead of failing.
globalThis.window = { localStorage: { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } } };
assert.equal(automaticDefault('s1', true), true);
assert.equal(automaticDefault('s1', false), false);
rememberAutomatic('s1', true);

console.log('OK');
