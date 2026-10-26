// Unit pin for the Screen view's reconnect decisions (#1603): the backoff
// steps and their bound, and which monitor reopens. screen-reconnect.js keeps
// them as plain functions; the timers and the socket are pinned by the e2e
// test with a fake clock. Invoked by tests/unit/desktop/test_screen_reconnect.py.

import assert from 'node:assert/strict';

const {
  RECONNECT_DELAYS_MS, reconnectDelay, monitorChoice, parseMonitorChoice, chooseMonitor,
} = await import('../../app/webapp/static/screen/screen-reconnect.js');

// Backoff: grows, is capped, and ends.
assert.deepEqual(RECONNECT_DELAYS_MS.slice(0, 4), [1000, 2000, 4000, 8000]);
assert.ok(Math.max(...RECONNECT_DELAYS_MS) <= 15000, 'the wait is capped at 15 s');
for (let i = 1; i < RECONNECT_DELAYS_MS.length; i++) {
  assert.ok(RECONNECT_DELAYS_MS[i] >= RECONNECT_DELAYS_MS[i - 1], 'never shrinks');
}
const total = RECONNECT_DELAYS_MS.reduce((a, b) => a + b, 0);
assert.ok(total >= 45000 && total <= 75000, 'gives up after about a minute of waiting: ' + total);
assert.equal(reconnectDelay(0), 1000);
assert.equal(reconnectDelay(RECONNECT_DELAYS_MS.length - 1), 15000);
assert.equal(reconnectDelay(RECONNECT_DELAYS_MS.length), null, 'the budget is spent');
assert.equal(reconnectDelay(-1), null);

// The stored monitor: round-trips, and rejects anything that is not one.
const main = { id: 0, left: 0, top: 0, width: 1920, height: 1080, primary: true };
const above = { id: 1, left: 0, top: -1080, width: 1920, height: 1080, primary: false };
assert.deepEqual(monitorChoice(above), {
  id: 1, left: 0, top: -1080, width: 1920, height: 1080,
});
assert.deepEqual(parseMonitorChoice(JSON.stringify(monitorChoice(above))), monitorChoice(above));
for (const bad of [null, '', 'nope', '[]', '{}', '{"id":1}', '{"id":"1","left":0,"top":0,"width":1,"height":1}', '3']) {
  assert.equal(parseMonitorChoice(bad), null, 'rejects ' + bad);
}

// Choosing: rect first, then id, then the primary.
const both = [main, above];
assert.equal(chooseMonitor(both, monitorChoice(above)), above);
assert.equal(chooseMonitor(both, null), main, 'nothing stored: the primary');
assert.equal(chooseMonitor([], monitorChoice(above)), null);
// The layout changed and the ids swapped: the rectangle still finds it.
const swapped = [
  { id: 0, left: 0, top: -1080, width: 1920, height: 1080, primary: false },
  { id: 1, left: 0, top: 0, width: 1920, height: 1080, primary: true },
];
assert.equal(chooseMonitor(swapped, monitorChoice(above)), swapped[0]);
// The rectangle is gone (a resolution change) but the id exists: the id.
const resized = [main, { id: 1, left: 0, top: -900, width: 1600, height: 900, primary: false }];
assert.equal(chooseMonitor(resized, monitorChoice(above)), resized[1]);
// Neither exists: the primary, no error.
assert.equal(chooseMonitor([main], monitorChoice(above)), main);
// No primary flagged: the first.
assert.equal(chooseMonitor([{ ...above, primary: false }], null).id, 1);

console.log('OK');
