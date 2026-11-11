// Pin for the Background tasks sheet's state module
// (static/background-tasks/task-state.js): a running task and a plain
// success show no chip, every other ended status shows itself, and a
// failure is never neutral. Prints the declared values for the Python side
// to compare with src/session_view/task_state.py.
// Invoked by tests/unit/transcript/test_task_state.py.

import assert from 'node:assert/strict';

const { TaskPhase, COMPLETED, statusChip } =
  await import('../../app/webapp/static/background-tasks/task-state.js');

assert.ok(Object.isFrozen(TaskPhase));

assert.equal(statusChip({ status: TaskPhase.RUNNING }), null);
assert.equal(statusChip({ status: COMPLETED }), null);
assert.equal(statusChip(null), null);
assert.deepEqual(statusChip({ status: 'failed' }), { label: 'failed', tone: 'danger' });
assert.deepEqual(statusChip({ status: 'killed' }), { label: 'killed', tone: 'attention' });
assert.deepEqual(statusChip({ status: TaskPhase.FINISHED }), { label: 'finished', tone: 'neutral' });

console.log('VALUES ' + JSON.stringify(Object.values(TaskPhase).sort()));
console.log('COMPLETED ' + JSON.stringify(COMPLETED));
console.log('OK');
