// Unit pin for build-watch.js's decisions: whether the live build differs from
// the one the page booted on, and whether a trigger should read the server.
// Unknown is never stale. Invoked by tests/unit/static_ui/test_node_module_helpers.py.

import '../_browser_stubs.mjs';
import assert from 'node:assert/strict';

const { verdict, shouldCheck, CHECK_THROTTLE_MS, CHECK_INTERVAL_MS } =
  await import('../../../app/webapp/static/build-watch.js');

assert.equal(CHECK_THROTTLE_MS, 60000);
assert.equal(CHECK_INTERVAL_MS, 300000);

// Stale only when both sides are known and differ.
assert.equal(verdict('aaa', 'bbb'), 'stale');
assert.equal(verdict('aaa', 'aaa'), 'current');
for (const unknown of [null, undefined, '', 0]) {
  assert.equal(verdict('aaa', unknown), 'unknown');
  assert.equal(verdict(unknown, 'bbb'), 'unknown');
}
assert.equal(verdict(null, ''), 'unknown');

// Throttle: a first check always reads; later ones wait out the window.
assert.equal(shouldCheck(1000, null, 60000), true);
assert.equal(shouldCheck(1000, undefined, 60000), true);
assert.equal(shouldCheck(30000, 1000, 60000), false);
assert.equal(shouldCheck(61000, 1000, 60000), true);
assert.equal(shouldCheck(60999, 1000, 60000), false);

console.log('OK');
