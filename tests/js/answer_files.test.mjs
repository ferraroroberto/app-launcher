// Unit pin for files on the Board answer sheets: the message lines they add for
// the chief, the file count an owner-check answer posts (and nothing else about
// the files), and the per-item and per-sheet caps. Synthetic names and paths
// only. Plain Node against the real ES modules, browser globals stubbed first.
// Invoked by tests/unit/board/test_owner_checks.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { composeAnswers } = await import('../../app/webapp/static/board/board-answers.js');
const { ownerAnswerOf, ownerMessageLine, emptyOwnerDraft, BEFORE_START, VALIDATE_AFTER } =
  await import('../../app/webapp/static/board/owner-checks.js');
const { MAX_ITEM_FILES, MAX_SHEET_FILES, attachedLines, roomFor } =
  await import('../../app/webapp/static/board/answer-files.js');

const SEP = ' → ';
const shot = { name: 'shot.png', path: '/tmp/demo/shot.png' };
const log = { name: 'log.txt', path: '/tmp/demo/log.txt' };
const items = [
  { repo: 'demo', ref: 'demo#1', question: 'Which one?', id: 'q-one', options: [{ label: 'A' }, { label: 'B' }] },
  { repo: 'demo', ref: 'demo#2', question: 'Why?', options: [] },
  { repo: 'demo', ref: 'demo#3', question: 'Anything?', options: [] },
];

// --- the chief sheet: a path rides under its item's line --------------------

const drafts = [
  { picks: [0], other: '', files: [shot, log] },
  { picks: [], other: '', files: [shot] },
  { picks: [], other: '', files: [] },
];
assert.equal(
  composeAnswers(items, drafts, '', [0, 1, 2], []),
  [
    'Answers from the Board (2 of 3):',
    '1. [demo#1] Which one?' + SEP + 'A {id: q-one}',
    '   Attached: /tmp/demo/shot.png',
    '   Attached: /tmp/demo/log.txt',
    '2. [demo#2] Why?' + SEP + '(files only)',
    '   Attached: /tmp/demo/shot.png',
    'Skipped: 3',
  ].join('\n'),
  'files ride under their item; files alone answer; only an item with neither is skipped');

// No files: the line keeps the shape it had before files existed.
assert.equal(
  composeAnswers(items, [
    { picks: [1], other: '', files: [] },
    { picks: [], other: 'x', files: [] },
    { picks: [], other: '', files: [] },
  ], '', [0, 1], []),
  ['Answers from the Board (2 of 2):', '1. [demo#1] Which one?' + SEP + 'B {id: q-one}',
    '2. [demo#2] Why?' + SEP + 'Other: x'].join('\n'));

// --- the owner sheet: a count to GitHub, the paths to the chief ----------------

const validate = { ref: 'demo#9', kind: VALIDATE_AFTER, title: 'New tab' };
const before = { ref: 'demo#7', kind: BEFORE_START, title: 'Pair it' };
const draft = (extra) => Object.assign(emptyOwnerDraft(), extra);

const typed = ownerAnswerOf(validate, draft({ other: 'the tab is blank', files: [shot, log] }));
assert.deepEqual(typed, {
  ref: 'demo#9', kind: VALIDATE_AFTER, answer: 'problem', text: 'the tab is blank', attachments: 2,
});
assert.ok(!JSON.stringify(typed).includes('/tmp'), 'the posted answer holds no path');
assert.equal(
  ownerMessageLine(validate, typed, [shot, log]),
  '[demo#9] Problem found validating New tab: the tab is blank (please file a fix)\n'
  + '   Attached: /tmp/demo/shot.png\n   Attached: /tmp/demo/log.txt');

const done = ownerAnswerOf(before, draft({ pick: 'done', files: [shot] }));
assert.equal(done.attachments, 1);
assert.equal(ownerMessageLine(before, done, [shot]), '[demo#7] Before-start step done: Pair it\n   Attached: /tmp/demo/shot.png');

// Every answer carries its files, and files with no answer make no answer at all.
const pending = ownerAnswerOf(before, draft({ pick: 'pending', files: [shot] }));
assert.equal(pending.attachments, 1);
assert.equal(
  ownerMessageLine(before, pending, [shot]),
  '[demo#7] Before-start: pending - Pair it\n   Attached: /tmp/demo/shot.png');
assert.equal(ownerAnswerOf(validate, draft({ files: [shot] })), null);

// --- caps ----------------------------------------------------------------------

assert.deepEqual(attachedLines([]), []);
assert.equal(roomFor(0, 0), MAX_ITEM_FILES);
assert.equal(roomFor(MAX_ITEM_FILES, 0), 0, 'an item stops at its cap');
assert.equal(roomFor(2, MAX_SHEET_FILES - 1), 1, 'the sheet cap can bind before the item cap');
assert.equal(roomFor(0, MAX_SHEET_FILES + 3), 0, 'never negative');
assert.equal(MAX_ITEM_FILES, 5);
assert.equal(MAX_SHEET_FILES, 30);

console.log('OK answer files');
