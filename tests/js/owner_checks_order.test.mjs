// Unit pin for the Board's owner-check order: waiting before answered, each
// half newest first by `since`, a higher issue number winning a tie, items with
// no date last. Synthetic data only. Invoked by tests/unit/board/test_owner_checks.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { els } = await import('../../app/webapp/static/state.js');
els.boardDispatchRepo = { value: '' }; // no repo filter
const { shownOwnerItems } = await import('../../app/webapp/static/board/owner-checks.js');

const item = (number, since, mark) => ({ repo: 'demo', number, ref: 'demo#' + number, since, mark });
const body = {
  owner_checks: {
    available: true,
    items: [
      item(1, '2026-10-01'),
      item(2, ''),
      item(7, '2026-11-03'),
      item(5, '2026-11-03'),
      item(3, '2026-10-20', 'later'),
      item(4, '2026-10-25', 'problem'),
      item(6, '2026-10-15'),
    ],
  },
};

const order = shownOwnerItems(body).map((it) => it.number);
assert.deepEqual(order, [7, 5, 6, 1, 2, 4, 3]);

// The shared date helpers behind the owner rows and the question rows.
const { shortDate, newestDateFirst } = await import('../../app/webapp/static/shared/format.js');
const thisYear = new Date().getFullYear();
assert.equal(shortDate(thisYear + '-11-04'), '4 Nov');
assert.equal(shortDate('2020-01-15'), '15 Jan 2020');
assert.equal(shortDate(''), '');
assert.equal(shortDate('soon'), '');
assert.equal(shortDate('2026-13-01'), '');
const asked = ['', '2026-11-01', 'junk', '2026-11-03'];
assert.deepEqual(asked.slice().sort(newestDateFirst), ['2026-11-03', '2026-11-01', '', 'junk']);

console.log('owner checks order: ok');
