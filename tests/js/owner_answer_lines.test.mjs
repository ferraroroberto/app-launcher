// Unit pin for the owner-check answer builder: which answer each choice makes and
// the exact line the chief reads for it. The chief's SKILL parses these lines,
// so a reworded one is a breaking change. Synthetic refs only. Plain Node against
// the real ES module, browser globals stubbed first.
// Invoked by tests/unit/board/test_owner_checks.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { ownerAnswerOf, ownerMessageLine, ownerMeaning, ownerNeedsChief, emptyOwnerDraft, BEFORE_START, VALIDATE_AFTER } =
  await import('../../app/webapp/static/board/owner-checks.js');

const before = { ref: 'demo#7', kind: BEFORE_START, title: 'Pair it' };
const validate = { ref: 'demo#9', kind: VALIDATE_AFTER, title: 'New tab' };
const draft = (extra) => Object.assign(emptyOwnerDraft(), extra);
const line = (item, extra) => {
  const ans = ownerAnswerOf(item, draft(extra));
  return ans && ownerMessageLine(item, ans, []);
};

// --- before start: one distinct line per choice -------------------------------

assert.equal(line(before, { pick: 'done' }), '[demo#7] Before-start step done: Pair it');
assert.equal(line(before, { pick: 'pending' }), '[demo#7] Before-start: pending - Pair it');
assert.equal(line(before, { pick: 'pause' }), '[demo#7] Before-start: pause - Pair it');
assert.equal(line(before, { pick: 'close' }), '[demo#7] Before-start: close - Pair it');

// The Other text rides with a pick as its note, whitespace folded.
assert.equal(
  line(before, { pick: 'pause', other: 'wait  for\nthe new phone' }),
  '[demo#7] Before-start: pause - Pair it (note: wait for the new phone)');
assert.equal(line(before, { pick: 'done', other: 'paired' }), '[demo#7] Before-start step done: Pair it (note: paired)');

// Text alone on a before-start step is a Pending note: a question for the chief
// leaves the step open.
assert.equal(
  line(before, { other: 'what was that again?' }),
  '[demo#7] Before-start: pending - Pair it (note: what was that again?)');
assert.equal(line(before, {}), null, 'nothing picked and nothing typed makes no answer');

// --- validate after ------------------------------------------------------------

assert.equal(line(validate, { pick: 'validated' }), '[demo#9] Validated: New tab');
assert.equal(line(validate, { pick: 'validated', other: 'on the phone' }), '[demo#9] Validated: New tab (note: on the phone)');
assert.equal(
  line(validate, { pick: 'problem', other: 'the tab is blank' }),
  '[demo#9] Problem found validating New tab: the tab is blank (please file a fix)');
assert.equal(line(validate, { pick: 'problem' }), null, 'a problem needs its description');
assert.equal(line(validate, { pick: 'problem', other: '   ' }), null);

// Typed text with no pick is read: works validates, anything else is a problem.
assert.equal(line(validate, { other: 'works fine' }), '[demo#9] Validated: New tab (note: works fine)');
assert.equal(
  line(validate, { other: 'does not work' }),
  '[demo#9] Problem found validating New tab: does not work (please file a fix)');

// A pick wins over the text's wording: Problem found stays a problem.
assert.equal(ownerAnswerOf(validate, draft({ pick: 'problem', other: 'works fine' })).answer, 'problem');

// --- the answer posted to GitHub's route and the chief gate ---------------------

assert.deepEqual(ownerAnswerOf(before, draft({ pick: 'close' })), { ref: 'demo#7', kind: BEFORE_START, answer: 'close' });
const withFiles = ownerAnswerOf(before, draft({ pick: 'pending', files: [{ name: 'a', path: '/tmp/a' }] }));
assert.equal(withFiles.attachments, 1);
assert.ok(!JSON.stringify(withFiles).includes('/tmp'), 'the posted answer holds no path');

const needs = (answer, files = []) => ownerNeedsChief({ answer }, files);
assert.equal(needs('validated'), false);
assert.equal(needs('done'), false);
assert.equal(needs('pending'), false);
assert.equal(needs('problem'), true);
assert.equal(needs('pause'), true);
assert.equal(needs('close'), true);
assert.equal(needs('pending', [{ name: 'a', path: '/tmp/a' }]), true, 'files need the chief session');

// --- the line under the field says which way it goes ----------------------------

assert.equal(ownerMeaning(validate, draft({})), '');
assert.equal(ownerMeaning(validate, draft({ pick: 'problem' })), 'Problem found needs a description in Other.');
assert.equal(ownerMeaning(validate, draft({ other: 'works' })), 'Sends Validated, with this as a note.');
assert.equal(ownerMeaning(validate, draft({ other: 'broken' })), 'Sends a problem report: the chief files a fix.');
assert.equal(ownerMeaning(validate, draft({ pick: 'problem', other: 'broken' })), 'Sends a problem report: the chief files a fix.');
assert.equal(ownerMeaning(before, draft({ pick: 'done' })), '');
assert.equal(ownerMeaning(before, draft({ other: 'later' })), 'Sends Pending, with this as a note.');
assert.equal(ownerMeaning(before, draft({ pick: "close", other: 'x' })), "Sends Close (won't do), with this as a note.");

console.log('OK owner answer lines');
