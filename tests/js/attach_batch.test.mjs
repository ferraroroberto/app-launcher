// Unit pin for the composer's attach queue (#1354). Runs under plain Node
// against the DOM-free module the composer imports. Invoked by
// tests/test_attach_batch.py.

import assert from 'node:assert/strict';
import {
  MAX_UPLOAD_BYTES, createAttachQueue, formatMB, oversizeReason, summarize,
} from '../../app/webapp/static/attach-batch.js';

const MB = 1024 * 1024;
const file = (name, size = 1000) => ({ name, size });

// ---- size check and wording -------------------------------------------------
assert.equal(MAX_UPLOAD_BYTES, 12 * MB);
assert.equal(formatMB(12 * MB), '12 MB');
assert.equal(formatMB(14.3 * MB), '14.3 MB');
assert.equal(oversizeReason(file('a.jpg', 12 * MB)), '', 'exactly the limit is allowed');
assert.equal(oversizeReason(file('a.jpg', 12 * MB + 1)), '12 MB, limit 12 MB');
assert.equal(oversizeReason(file('a.jpg', 14.3 * MB)), '14.3 MB, limit 12 MB');
assert.equal(oversizeReason(null), '');

assert.deepEqual(summarize(1, 1, []), {
  text: 'Uploaded 1 file — path added to the message.', tone: 'good',
});
assert.deepEqual(summarize(3, 3, []), {
  text: 'Uploaded 3 files — paths added to the message.', tone: 'good',
});
assert.deepEqual(summarize(9, 10, [{ name: 'IMG_1.jpg', reason: 'file exceeds 12 MB' }]), {
  text: 'Uploaded 9 of 10 — 1 failed: IMG_1.jpg (file exceeds 12 MB)', tone: 'error',
});
assert.equal(
  summarize(0, 5, ['a', 'b', 'c', 'd', 'e'].map((n) => ({ name: n, reason: 'x' }))).text,
  'Uploaded 0 of 5 — 5 failed: a (x), b (x), c (x), and 2 more',
  'names three, counts the rest',
);

// ---- the queue --------------------------------------------------------------
// A controllable upload: each call parks until the test settles it.
function harness() {
  const h = { appended: [], progress: [], settled: [], logs: [], calls: [], gates: [] };
  h.queue = createAttachQueue({
    upload: (f) => new Promise((resolve, reject) => {
      h.calls.push(f.name);
      h.gates.push({ resolve, reject });
    }),
    onAppend: (p) => h.appended.push(p),
    onProgress: (p) => h.progress.push(p),
    onSettle: (r) => h.settled.push(r),
    log: (e) => h.logs.push(e),
  });
  return h;
}
const tick = () => new Promise((r) => setImmediate(r));

{
  // In order, one at a time, one summary.
  const h = harness();
  const drained = h.queue.enqueue([file('a'), file('b')]);
  await tick();
  assert.deepEqual(h.calls, ['a'], 'only the first is in flight');
  assert.equal(h.queue.busy(), true);
  h.gates[0].resolve('/p/a');
  await tick();
  assert.deepEqual(h.calls, ['a', 'b']);
  h.gates[1].resolve('/p/b');
  await drained;
  assert.deepEqual(h.appended, ['/p/a', '/p/b']);
  assert.deepEqual(h.progress, [
    { index: 1, total: 2, name: 'a' }, { index: 2, total: 2, name: 'b' }, null,
  ]);
  assert.equal(h.settled.length, 1, 'one summary per run');
  assert.deepEqual(h.settled[0], { ok: 2, total: 2, failures: [] });
  assert.equal(h.queue.busy(), false);
  assert.deepEqual(h.logs.map((e) => [e.name, e.ok]), [['a', true], ['b', true]]);
}

{
  // A pick mid-batch queues behind it: the total grows, order is pick order,
  // nothing starts beside the running upload, nothing is sent twice.
  const h = harness();
  const first = h.queue.enqueue([file('a'), file('b')]);
  await tick();
  const second = h.queue.enqueue([file('c')]);
  assert.equal(second, first, 'the queued pick joins the same run');
  await tick();
  assert.deepEqual(h.calls, ['a']);
  h.gates[0].resolve('/p/a'); await tick();
  h.gates[1].resolve('/p/b'); await tick();
  h.gates[2].resolve('/p/c');
  await first;
  assert.deepEqual(h.calls, ['a', 'b', 'c']);
  assert.deepEqual(h.appended, ['/p/a', '/p/b', '/p/c']);
  assert.deepEqual(
    h.progress.map((p) => (p ? p.index + '/' + p.total : null)),
    ['1/2', '1/3', '2/3', '3/3', null],
  );
  assert.equal(h.settled.length, 1);
}

{
  // A failure mid-batch (a throw, and a null) is recorded; the rest land.
  const h = harness();
  const drained = h.queue.enqueue([file('a'), file('b'), file('c')]);
  await tick(); h.gates[0].resolve('/p/a');
  await tick(); h.gates[1].reject(new Error('disk full'));
  await tick(); h.gates[2].resolve(null);
  await drained;
  assert.deepEqual(h.appended, ['/p/a']);
  assert.deepEqual(h.settled[0], {
    ok: 1, total: 3,
    failures: [{ name: 'b', reason: 'disk full' }, { name: 'c', reason: 'upload failed' }],
  });
  assert.deepEqual(h.logs.map((e) => e.ok), [true, false, false]);
  assert.equal(h.logs[1].reason, 'disk full');
}

{
  // An oversize file is never handed to upload; the others still go.
  const h = harness();
  const drained = h.queue.enqueue([file('big', 14 * MB), file('a')]);
  await tick();
  assert.deepEqual(h.calls, ['a'], 'the oversize file is not uploaded');
  h.gates[0].resolve('/p/a');
  await drained;
  assert.deepEqual(h.settled[0], {
    ok: 1, total: 2, failures: [{ name: 'big', reason: '14 MB, limit 12 MB' }],
  });
}

{
  // After a drain the counters start over, and an empty pick does nothing.
  const h = harness();
  const one = h.queue.enqueue([file('a')]);
  await tick(); h.gates[0].resolve('/p/a'); await one;
  await h.queue.enqueue([]);
  const two = h.queue.enqueue([file('b')]);
  await tick(); h.gates[1].resolve('/p/b'); await two;
  assert.deepEqual(h.settled.map((r) => r.total), [1, 1]);
  assert.deepEqual(h.progress[2], { index: 1, total: 1, name: 'b' });
}

console.log('OK');
