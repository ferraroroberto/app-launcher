// Unit pin for the Chat send confirmation (#1744): the outcome mapping in
// sessions.js::sendOutcome and the evidence judge in send-confirm.js. Plain
// Node against the real ES modules; the browser globals sessions.js's import
// graph touches at load are stubbed first. Invoked by tests/unit/board/test_send_confirm.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { sendOutcome, NOT_PICKED_UP } = await import('../../app/webapp/static/sessions.js');
const {
  CONFIRM_WINDOW_MS, alreadyPending, countMatchingTurns, judgeWatch, pendingNotTaken, startWatch,
} = await import('../../app/webapp/static/chat/send-confirm.js');

// --- the outcome mapping -------------------------------------------------

// An unconfirmed send says "Sent" and asks for the follow-up check, but only
// for a caller that will run it; any other caller keeps the old warning.
assert.deepEqual(
  sendOutcome({ submit_state: 'unconfirmed', delivered: true }, { watch: true }),
  { text: 'Sent', kind: '', watch: true });
assert.match(sendOutcome({ submit_state: 'unconfirmed', delivered: true }).text, /not confirmed/);
assert.equal(sendOutcome({ submit_state: 'unconfirmed', delivered: true }).watch, undefined);

// Every other verdict keeps its own wording, watched or not.
for (const opts of [undefined, { watch: true }]) {
  assert.deepEqual(sendOutcome({ submit_state: 'confirmed' }, opts), { text: 'Sent', kind: 'good' });
  assert.match(sendOutcome({ submit_state: 'pending' }, opts).text, /^Queued/);
  assert.match(sendOutcome({ submit_state: 'not_submitted' }, opts).text, /^Not submitted/);
  assert.equal(sendOutcome({ submit_state: 'not_submitted' }, opts).kind, 'error');
  // A detached console has no transcript to watch: it says what it cannot know.
  const detached = sendOutcome({ delivered: 'unconfirmed', submit_state: 'unconfirmed' }, opts);
  assert.equal(detached.text, 'Sent, not confirmed: typed into the PC console');
  assert.equal(detached.watch, undefined);
}

// The not-picked-up state is its own wording and an error, never plain "Sent".
assert.equal(NOT_PICKED_UP.kind, 'error');
assert.match(NOT_PICKED_UP.text, /hasn’t picked it up/);

// --- the evidence judge --------------------------------------------------

const user = (text, extra) => ({ kind: 'user', text, ...extra });
const assistant = (text) => ({ kind: 'assistant', text });

// A matching new user turn confirms; whitespace and a truncated entry do not matter.
assert.equal(countMatchingTurns([user('hello  world'), assistant('hi')], 'hello world'), 1);
assert.equal(countMatchingTurns([user('a'.repeat(40) + ' tail cut')], 'a'.repeat(40) + ' different tail'), 1);
assert.equal(countMatchingTurns([user('something else')], 'hello'), 0);
assert.equal(countMatchingTurns([user('hello', { sidechain: true })], 'hello'), 0);
assert.equal(countMatchingTurns(null, 'hello'), 0);
// A slash command is logged as a command entry, so any new user turn vouches for it.
assert.equal(countMatchingTurns([user('<command-name>/compact</command-name>')], '/compact'), 1);

const T0 = 1_000_000;
const before = [user('earlier'), assistant('ok')];

let w = startWatch('yes', before, false, T0);
assert.equal(w.deadline, T0 + CONFIRM_WINDOW_MS);
// Nothing yet, window open: waiting, and never a warning before it has looked.
assert.equal(judgeWatch(w, before, false, T0 + 1000), 'waiting');
// The turn shows up: picked up, well inside the window.
assert.equal(judgeWatch(w, [...before, user('yes')], false, T0 + 4000), 'picked-up');
// The agent going busy is evidence too.
assert.equal(judgeWatch(w, before, true, T0 + 4000), 'picked-up');
// Window closed with neither: the distinct state.
assert.equal(judgeWatch(w, before, false, T0 + CONFIRM_WINDOW_MS), 'not-picked-up');
assert.equal(judgeWatch(w, before, false, T0 + CONFIRM_WINDOW_MS - 1), 'waiting');
// Evidence found on the closing read still wins over the deadline.
assert.equal(judgeWatch(w, [...before, user('yes')], false, T0 + CONFIRM_WINDOW_MS + 5000), 'picked-up');

// An earlier identical message does not vouch for this one.
const said = [user('yes'), assistant('done')];
w = startWatch('yes', said, false, T0);
assert.equal(judgeWatch(w, said, false, T0 + CONFIRM_WINDOW_MS), 'not-picked-up');
assert.equal(judgeWatch(w, [...said, user('yes')], false, T0 + 3000), 'picked-up');

// An agent already working at send time says nothing about this message.
w = startWatch('next', before, true, T0);
assert.equal(judgeWatch(w, before, true, T0 + 3000), 'waiting');
assert.equal(judgeWatch(w, before, true, T0 + CONFIRM_WINDOW_MS), 'not-picked-up');
assert.equal(judgeWatch(w, [...before, user('next')], true, T0 + 3000), 'picked-up');

// --- messages still pending ----------------------------------------------

const AT = 1_800_000_000;  // epoch seconds, as the session-host stamps them
const iso = (sec) => new Date(sec * 1000).toISOString();
const queued = { id: 'a', text: 'also check  the ferry\ntimes', at: AT };
const slash = { id: 'b', text: '/compact', at: AT };
const turn = (text, sec) => ({ kind: 'user', text, timestamp: iso(sec) });

// Not taken until a matching user turn appears after it was sent.
assert.deepEqual(pendingNotTaken([queued], []), [queued]);
assert.deepEqual(pendingNotTaken([queued], [turn('also check the ferry times', AT + 20)]), []);
// The same words said well before the send are a different message.
assert.deepEqual(pendingNotTaken([queued], [turn('also check the ferry times', AT - 600)]), [queued]);
// A slash command is recorded as a command, never as its text: only the host can clear it.
assert.deepEqual(pendingNotTaken([slash], [turn('/compact', AT + 5)]), [slash]);
// A session that does not track pending messages has none to show.
assert.deepEqual(pendingNotTaken(null, []), []);

// The double-send check compares the whole text, whitespace aside.
assert.equal(alreadyPending([queued], 'also check the ferry times'), true);
assert.equal(alreadyPending([queued], 'also check the ferry'), false);
assert.equal(alreadyPending([queued], '   '), false);
assert.equal(alreadyPending(null, 'x'), false);

console.log('OK');
