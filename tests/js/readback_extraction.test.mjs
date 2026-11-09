// Pins for the read-aloud reply segmenter, under plain Node: the last "●"
// assistant block is de-wrapped and read, and the composer box, status footer,
// recap, timing line and live spinner are not. The segmenter is pure over
// {text, marker} rows (the marker is what the live cell-colour reader derives:
// 'assistant' = default/white bullet, 'tool' = coloured bullet, 'none').
// Invoked by tests/unit/static_ui/test_node_module_helpers.py.

import './_browser_stubs.mjs';
import assert from 'node:assert/strict';

const { extractReplyBlocksFromRows } =
  await import('../../app/webapp/static/terminal/terminal-readback.js');

const M = { a: 'assistant', t: 'tool', n: 'none' };
const rows = (...pairs) => pairs.map(([m, text]) => ({ text, marker: M[m] }));
const lastReply = (r) => {
  const b = extractReplyBlocksFromRows(r);
  return b.length ? b[b.length - 1] : '';
};

// An idle rendering at the phone's 51-col width: one reply block, then the
// timing line, a recap, the composer box and the status footer.
const IDLE = rows(
  ['n', '                    > ship it'],
  ['n', ''],
  ['a', '● Done — issue #190 is built, verified, and'],
  ['n', '  live on your phone. The pre-ship gate is'],
  ['n', '  green and the webapp is live on :8445.'],
  ['n', ''],
  ['n', '  Worked for 21m 17s'],
  ['n', ''],
  ['n', '  recap: Goal: add an eyes-free read-aloud'],
  ['n', '  button to the Coding tab (issue #190).'],
  ['n', '  (disable recaps in /config)'],
  ['n', ''],
  ['n', '          ─────────────────────────────────'],
  ['n', '          ──────── voice drive from mobile ──'],
  ['n', '          > '],
  ['n', '          ─────────────────────────────────'],
  ['n', '  app-launcher (feat/190-read-last-reply-aloud) …'],
  ['n', '  ⏵⏵ bypass permissions on (shift+tab to cycle)'],
  ['n', '                              187754 tokens'],
);
assert.equal(
  lastReply(IDLE),
  'Done — issue #190 is built, verified, and live on your phone. '
  + 'The pre-ship gate is green and the webapp is live on :8445.',
);

// Mid-work with no reply yet: a spinner and a pending tool, no assistant block.
const RUNNING = rows(
  ['n', '                    > do the thing'],
  ['n', ''],
  ['n', '  ⎿  Waiting…'],
  ['n', ''],
  ['n', '  Ruminating… (2m 3s · ↓ 7.2k tokens)'],
  ['n', ''],
  ['n', '          ────────────────────────'],
  ['n', '          > '],
  ['n', '          ────────────────────────'],
  ['n', '  app-launcher (main) …'],
  ['n', '  Ruminating…  ✶  187754 tokens'],
);
assert.equal(lastReply(RUNNING), '');

// The live thinking spinner has no bullet, so the last completed reply above
// it is read; a de-wrap keeps the soft-wrap hyphen seam.
const THINKING = rows(
  ['a', "● Key finding: there's a maintained chatterbox-"],
  ['n', '  tts PyPI package with the exact API I need.'],
  ['n', ''],
  ['t', '● Read(E:\automation\local-llm-hub\src\whisper_'],
  ['n', '  translate_proxy.py)'],
  ['n', '  ⎿  Read 386 lines'],
  ['n', ''],
  ['n', '✻ Cogitating… (4m 39s · thinking)'],
  ['n', ''],
  ['n', '          ─────────────────────────────────'],
  ['n', '          > '],
  ['n', '          ─────────────────────────────────'],
  ['n', '  12% | local-llm-hub (feat/98-tts-backend-audio…'],
  ['n', '  ▶▶ auto mode on (shift+tab to cycle)'],
  ['n', '                              127496 tokens'],
);
assert.equal(
  lastReply(THINKING),
  "Key finding: there's a maintained chatterbox- tts PyPI package with the exact API I need.",
);

// The spinner's "⎿ Tip:" child is trailing continuation of the last bullet; the
// block truncates at the spinner so the real reply is read, not the tip.
const TIP = rows(
  ['n', '  ⎿  Allowed by auto mode classifier'],
  ['n', ''],
  ['a', "● Everything's wired. Now docs — the README is"],
  ['n', '  explicitly part of what you asked for ("put into'],
  ['n', '  the readme how to connect and consume from other'],
  ['n', '  apps"). Let me write docs/add-tts.md and update'],
  ['n', '  the README.'],
  ['n', ''],
  ['n', '· Processing… (15m 11s · ↓ 64.6k tokens)'],
  ['n', '  ⎿  Tip: Running multiple Claude sessions? Use'],
  ['n', '     /color and /rename to tell them apart at a'],
  ['n', '     glance.'],
  ['n', ''],
  ['n', '          ─────────────────────────────────'],
  ['n', '          > '],
  ['n', '          ─────────────────────────────────'],
  ['n', '  23% | local-llm-hub (feat/98-tts-backend-audio…'],
  ['n', '  ▶▶ auto mode on (shift+tab to cycle)'],
  ['n', '                              225377 tokens'],
);
assert.equal(
  lastReply(TIP),
  'Everything\'s wired. Now docs — the README is explicitly part of what you '
  + 'asked for ("put into the readme how to connect and consume from other '
  + 'apps"). Let me write docs/add-tts.md and update the README.',
);

// A titled composer border dilutes the whole-line rule ratio, and a draft
// prompt sits inside the box; the box is cut and "Crunched for" ends the block.
const TITLED = rows(
  ['n', '                    > read the last reply'],
  ['a', '● The toast was rendering behind the terminal.'],
  ['n', '  Reload and tap the speaker again.'],
  ['n', ''],
  ['n', '✻ Crunched for 5s · 1 shell still running'],
  ['n', '─────────────── voice drive from mobile ───────'],
  ['n', '> see the Reading toast now'],
  ['n', '─────────────────────────────────────────────'],
  ['n', '28% | app-launcher (feat/190-read-last-reply-a…'],
  ['n', '▶▶ auto mode                          agents'],
  ['n', '                              30251 tokens'],
);
assert.equal(
  lastReply(TITLED),
  'The toast was rendering behind the terminal. Reload and tap the speaker again.',
);

// Every reply in order, tool blocks excluded.
assert.deepEqual(
  extractReplyBlocksFromRows(rows(
    ['a', '● First reply.'],
    ['t', '● Bash(ls)'],
    ['n', '  ⎿  ok'],
    ['a', '● Second reply, wrapped'],
    ['n', '  onto two lines.'],
    ['a', '● Third reply.'],
  )),
  ['First reply.', 'Second reply, wrapped onto two lines.', 'Third reply.'],
);

console.log('OK');
