// Unit pin for the Board card's date stamp (#1638): dom-utils.js fmtCardStamp.
// A clock time for today, a short date for any other day, always the viewer's
// local time, '' for a value that can't be read. Runs under plain Node;
// invoked by tests/test_card_stamp.py.

import assert from 'node:assert/strict';

globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const stub = () => new Proxy(function () {}, {
  get: (t, p) => (p === Symbol.toPrimitive ? () => '' : stub()),
  apply: () => stub(),
});
globalThis.document = stub();
globalThis.window = stub();

const { fmtCardStamp } = await import('../../app/webapp/static/dom-utils.js');

const now = new Date(2026, 9, 17, 18, 30);            // local 17 Oct 2026 18:30

// Today: the clock, 24 h, zero-padded.
assert.equal(fmtCardStamp(new Date(2026, 9, 17, 15, 29).toISOString(), now), '15:29');
assert.equal(fmtCardStamp(new Date(2026, 9, 17, 0, 5).toISOString(), now), '00:05');
// Another day: day + short month, no year, whatever the hour.
assert.equal(fmtCardStamp(new Date(2026, 9, 14, 23, 59).toISOString(), now), '14 Oct');
assert.equal(fmtCardStamp(new Date(2026, 9, 16, 23, 59).toISOString(), now), '16 Oct');
assert.equal(fmtCardStamp(new Date(2026, 0, 3, 8, 0).toISOString(), now), '3 Jan');
// Unreadable values give nothing, so the caller drops the part.
for (const bad of [null, undefined, '', 'not a date']) {
  assert.equal(fmtCardStamp(bad, now), '');
}

console.log('OK');
