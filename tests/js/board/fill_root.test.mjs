// Unit pin for the owner step's `<root>`: every occurrence becomes this PC's
// fleet root, and with no root the text is left as it was and said so.
// Synthetic paths only. Invoked by tests/unit/board/test_owner_checks.py.

import assert from 'node:assert/strict';

const { fillRoot } = await import('../../../app/webapp/static/board/fill-root.js');

const step = 'cd /d <root>\\demo && tray.bat\n<root>/demo/run.py --dir <root>';
assert.deepEqual(fillRoot(step, 'X:\\fleet'), {
  text: 'cd /d X:\\fleet\\demo && tray.bat\nX:\\fleet/demo/run.py --dir X:\\fleet',
  unfilled: false,
});
// A drive root never doubles the separator, and `$&` in a name stays literal.
assert.equal(fillRoot('<root>\\demo', 'X:\\').text, 'X:\\demo');
assert.equal(fillRoot('<root>\\demo', 'X:\\a$&b').text, 'X:\\a$&b\\demo');

// Unresolved: unchanged, and said only when there was something to fill.
for (const root of [null, undefined, '']) {
  assert.deepEqual(fillRoot('<root>\\demo', root), { text: '<root>\\demo', unfilled: true });
}
assert.deepEqual(fillRoot('tray.bat --restart', null), { text: 'tray.bat --restart', unfilled: false });

console.log('ok');
