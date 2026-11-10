// Unit pin for the Board idea sheet's one message to the chief (the contract in
// fleet-config docs/inspiration-ideas.md) and the repo picker's choices. Made-up
// ids, repos and paths only. Invoked by tests/unit/core/test_inspiration_ideas.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { composeIdeaMessage, repoChoices } = await import('../../app/webapp/static/board/idea-sheet.js');

const shot = { name: 'shot.png', path: '/tmp/demo/shot.png' };
const note = { name: 'note.txt', path: '/tmp/demo/note.txt' };

// File: the first line names the repo, then the edited prompt as typed, then one line per file.
assert.equal(
  composeIdeaMessage('abc123', 'file', 'demo', '  Do the thing.\n\n## Source\n- Link: x  ', [shot, note]),
  [
    'Idea from the Board: abc123 -> file in demo',
    'Do the thing.\n\n## Source\n- Link: x',
    'Attached: /tmp/demo/shot.png',
    'Attached: /tmp/demo/note.txt',
  ].join('\n'),
);

// Drop: the note after the line; an empty note adds no line, files still follow.
assert.equal(composeIdeaMessage('abc123', 'drop', 'demo', 'not for now', []),
  'Idea from the Board: abc123 -> drop\nnot for now');
assert.equal(composeIdeaMessage('abc123', 'drop', '', '   ', [shot]),
  'Idea from the Board: abc123 -> drop\nAttached: /tmp/demo/shot.png');

// The picker: the projects sorted, the suggestion first when it is not one of them.
assert.deepEqual(repoChoices(['zeta', 'alpha'], 'alpha'), ['alpha', 'zeta']);
assert.deepEqual(repoChoices(['zeta', 'alpha'], 'other'), ['other', 'alpha', 'zeta']);
assert.deepEqual(repoChoices([], ''), []);

console.log('OK idea message');
