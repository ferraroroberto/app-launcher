// Unit pin for format.js fitTitle: the Life OS Rename sheet pre-fills a
// conversation's long topic cut to the 120-character title limit. Runs under
// plain Node. Invoked by tests/unit/static_ui/test_fit_title.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { fitTitle } = await import('../../app/webapp/static/format.js');

const MAX = 120;
const words = 'plan the autumn trip around the lakes and book the slow ferry before the timetable changes';
const topic = Array(5).fill(words).join(' ') + ', then (confirm the return leg';   // ~460 chars

const cut = fitTitle(topic, MAX);
assert.ok(topic.length > 300);
assert.ok(cut.length <= MAX, `${cut.length} chars`);
assert.ok(topic.startsWith(cut), 'a prefix of the topic');
assert.ok(/^[ ,]/.test(topic.slice(cut.length)), 'ends on a whole word');
assert.ok(!/[\s,(]$/.test(cut), 'no trailing punctuation');

// Within the limit: unchanged, even at exactly the limit.
assert.equal(fitTitle('booking the ferry', MAX), 'booking the ferry');
assert.equal(fitTitle('x'.repeat(MAX), MAX), 'x'.repeat(MAX));
assert.equal(fitTitle('', MAX), '');
assert.equal(fitTitle(null, MAX), '');

// A space exactly at the limit keeps the whole word before it.
assert.equal(fitTitle('a'.repeat(MAX) + ' tail', MAX), 'a'.repeat(MAX));

// Trailing punctuation and an unclosed bracket go; a closed bracket stays.
assert.equal(fitTitle('alpha beta, gamma delta', 11), 'alpha beta');
assert.equal(fitTitle('alpha beta (gamma delta', 18), 'alpha beta');
assert.equal(fitTitle('alpha (beta) gamma delta', 18), 'alpha (beta) gamma');
assert.equal(fitTitle('alpha - beta - gamma', 8), 'alpha');

// One word longer than the limit is cut hard rather than to nothing.
assert.equal(fitTitle('y'.repeat(200), MAX), 'y'.repeat(MAX));

console.log('OK');
