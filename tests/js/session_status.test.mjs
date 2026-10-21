// Unit pin for the shared "status · age" formatter (#1580): the one function
// both the Board's session rows and the Code tab's rows print their status and
// last-activity age through. Runs under plain Node against the real ES module;
// the browser globals glance.js's import graph touches at load are stubbed
// first. Invoked by tests/test_session_status.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { sessionStatusBits, sessionStatusMeta } = await import('../../app/webapp/static/glance.js');

const MIN = 60;
const H = 3600;

// The Board's wording, status then age, for every status it prints.
assert.deepEqual(sessionStatusBits('working', 9 * MIN), ['working', '9m']);
assert.deepEqual(sessionStatusBits('idle-finished', 19 * H), ['finished', '19h']);
assert.deepEqual(sessionStatusBits('tool-pending', 30), ['running', 'now']);
assert.deepEqual(sessionStatusBits('awaiting-input', 5 * MIN), ['needs you', '5m']);
assert.deepEqual(sessionStatusBits('awaiting-decision', 5 * MIN), ['awaiting decision', '5m']);
assert.deepEqual(sessionStatusBits('idle', 2 * 86400), ['idle', '2d']);

// hideStatus keeps the age (a chip already says the word).
assert.deepEqual(sessionStatusBits('stalled', 5 * MIN, { hideStatus: true }), ['5m']);

// The chief's resting states read "standing by"; stalled does not.
assert.deepEqual(sessionStatusBits('awaiting-input', 5 * MIN, { chief: true }), ['standing by', '5m']);
assert.deepEqual(sessionStatusBits('idle-finished', 5 * MIN, { chief: true }), ['standing by', '5m']);
assert.deepEqual(sessionStatusBits('stalled', 5 * MIN, { chief: true }), ['stalled', '5m']);
assert.deepEqual(sessionStatusBits('working', 5 * MIN, { chief: true }), ['working', '5m']);

// A status with no word keeps the age; no age keeps the word.
assert.deepEqual(sessionStatusBits('unknown', 5 * MIN), ['5m']);
assert.deepEqual(sessionStatusBits('working', null), ['working']);

// No Board placement at all (null/undefined): nothing, never a guess.
assert.deepEqual(sessionStatusBits(null, 5 * MIN), []);
assert.deepEqual(sessionStatusBits(undefined, 5 * MIN), []);

// The row-state hook the Board paints is unchanged.
assert.equal(sessionStatusMeta('working', false).cls, 'is-working');
assert.equal(sessionStatusMeta('no-such-status', false).cls, 'is-unknown');

console.log('OK');
