// Pin for the shared sensitive-action wrapper (static/shared/sensitive-action.js):
// the pending toast fires before the request answers, a repeat tap on the same
// key sends nothing while one is in flight, and the outcome is done, failed or
// not confirmed. A timeout, or a failure with no HTTP answer, never reads as
// success. Driven with a delayed fake request and a recording notifier.
// Invoked by tests/unit/core/test_sensitive_action.py.

import '../_browser_stubs.mjs';
import assert from 'node:assert/strict';

const { SensitiveOutcome, isInFlight, runSensitiveAction } =
  await import('../../../app/webapp/static/shared/sensitive-action.js');

function recorder() {
  const calls = [];
  const notify = (text, kind) => calls.push({ text, kind: kind || '' });
  return { calls, notify };
}

function delayed(ms, value, fail) {
  return () => new Promise((resolve, reject) => {
    setTimeout(() => (fail ? reject(fail) : resolve(value)), ms);
  });
}

function base(extra) {
  return Object.assign({
    key: 'k', pending: 'Doing X…', failPrefix: 'X failed', unconfirmed: 'X not confirmed',
    done: () => 'X done',
  }, extra);
}

// Pending toast is shown synchronously, before the request answers.
{
  const { calls, notify } = recorder();
  const run = runSensitiveAction(base({ run: delayed(30, 1) }), notify);
  assert.deepEqual(calls.map((c) => c.text), ['Doing X…']);
  assert.ok(isInFlight('k'));
  const v = await run;
  assert.equal(v.outcome, SensitiveOutcome.DONE);
  assert.deepEqual(calls.map((c) => c.text), ['Doing X…', 'X done']);
  assert.ok(!isInFlight('k'));
}

// A second tap while the first is in flight sends nothing and toasts nothing.
{
  const { calls, notify } = recorder();
  let sent = 0;
  const run = () => { sent += 1; return delayed(30, 1)(); };
  const first = runSensitiveAction(base({ run }), notify);
  const second = await runSensitiveAction(base({ run }), notify);
  assert.equal(second.outcome, SensitiveOutcome.DROPPED);
  await first;
  assert.equal(sent, 1);
  assert.equal(calls.filter((c) => c.text === 'Doing X…').length, 1);
  // Once settled the target is free again.
  assert.equal((await runSensitiveAction(base({ run }), notify)).outcome, SensitiveOutcome.DONE);
  assert.equal(sent, 2);
}

// A different key is a different target.
{
  const { notify } = recorder();
  const a = runSensitiveAction(base({ key: 'a', run: delayed(20, 1) }), notify);
  const b = runSensitiveAction(base({ key: 'b', run: delayed(20, 1) }), notify);
  assert.equal((await a).outcome, SensitiveOutcome.DONE);
  assert.equal((await b).outcome, SensitiveOutcome.DONE);
}

// An HTTP failure is Failed and names the server's reason.
{
  const { calls, notify } = recorder();
  const err = Object.assign(new Error('no such session'), { status: 404 });
  const v = await runSensitiveAction(base({ run: delayed(5, null, err) }), notify);
  assert.equal(v.outcome, SensitiveOutcome.FAILED);
  assert.deepEqual(calls[calls.length - 1], { text: 'X failed: no such session', kind: 'error' });
  assert.ok(!isInFlight('k'));
}

// A request that outlives the timeout is Not confirmed, never done; the key
// stays held until it really settles, so a repeat tap cannot double-send.
{
  const { calls, notify } = recorder();
  let sent = 0;
  const run = () => { sent += 1; return delayed(120, 'late')(); };
  const v = await runSensitiveAction(base({ run, timeoutMs: 30 }), notify);
  assert.equal(v.outcome, SensitiveOutcome.UNCONFIRMED);
  assert.deepEqual(calls[calls.length - 1], { text: 'X not confirmed', kind: 'error' });
  assert.ok(!calls.some((c) => c.text === 'X done'));
  assert.ok(isInFlight('k'));
  assert.equal((await runSensitiveAction(base({ run, timeoutMs: 30 }), notify)).outcome,
    SensitiveOutcome.DROPPED);
  assert.equal(sent, 1);
  await new Promise((r) => setTimeout(r, 150));
  assert.ok(!isInFlight('k'));
}

// A failure with no HTTP answer (the connection died) is Not confirmed: the
// server may have acted.
{
  const { calls, notify } = recorder();
  const v = await runSensitiveAction(
    base({ run: delayed(5, null, new TypeError('Failed to fetch')) }), notify);
  assert.equal(v.outcome, SensitiveOutcome.UNCONFIRMED);
  assert.deepEqual(calls[calls.length - 1], { text: 'X not confirmed', kind: 'error' });
}

// A synchronous throw in run() is still a verdict, and frees the key.
{
  const { notify } = recorder();
  const v = await runSensitiveAction(base({ run: () => { throw new Error('boom'); } }), notify);
  assert.equal(v.outcome, SensitiveOutcome.UNCONFIRMED);
  assert.ok(!isInFlight('k'));
}

console.log('OK');
