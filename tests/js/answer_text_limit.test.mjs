// Unit pin for the answer text limit: a long dictated answer reaches the message
// the chief reads whole, and the field names its limit before Done. Synthetic
// text only. Plain Node against the real ES modules, browser globals stubbed first.
// Invoked by tests/unit/board/test_owner_checks.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { composeAnswers } = await import('../../app/webapp/static/board/board-answers.js');
const { ANSWER_TEXT_MAX, answerCountText } = await import('../../app/webapp/static/board/answer-item.js');
const { ownerAnswerOf, ownerMessageLine, emptyOwnerDraft, VALIDATE_AFTER } =
  await import('../../app/webapp/static/board/owner-checks.js');

// 5,000 characters of single-spaced words, as a dictation lands.
const long = Array.from({ length: 1100 }, (_, i) => 'w' + String(i % 10).repeat(3)).join(' ').slice(0, 5000).replace(/ $/, 'x');
assert.equal(long.length, 5000);

// --- a chief question: the whole text rides in the "Answers from the Board" line ---
const items = [{ question: 'Which way?', ref: 'demo#1', id: 'q-way', options: [] }];
const drafts = [{ picks: [], other: long, files: [] }];
const message = composeAnswers(items, drafts, '', null, [], '');
assert.equal(
  message,
  'Answers from the Board (1 of 1):\n1. [demo#1] Which way? → Other: ' + long + ' {id: q-way}');

// --- an owner check: the whole text rides in the line the chief reads ---
const item = { ref: 'demo#9', kind: VALIDATE_AFTER, title: 'New tab' };
const draft = Object.assign(emptyOwnerDraft(), { other: long });
const ans = ownerAnswerOf(item, draft);
assert.equal(ans.text, long);
assert.equal(
  ownerMessageLine(item, ans, []),
  '[demo#9] Problem found validating New tab: ' + long + ' (please file a fix)');

// --- the limit is shown before Done, never silent ---
assert.equal(ANSWER_TEXT_MAX, 20000);
assert.equal(answerCountText(5000), '', 'far from the limit: nothing to say');
assert.equal(answerCountText(ANSWER_TEXT_MAX - 2000), '2000 characters left');
assert.equal(answerCountText(ANSWER_TEXT_MAX), '0 characters left');
assert.match(answerCountText(ANSWER_TEXT_MAX + 7), /^7 characters over the 20000 limit/);

console.log('OK answer text limit');
