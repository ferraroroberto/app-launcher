// Pin for the Jobs tab's run-status module (static/jobs/run-status.js): only
// a pending or running run is live, so the tab never offers Kill or a live
// stream for a finished run, and a value outside the set is never live.
// Prints the declared values for the Python side to compare with RunStatus.
// Invoked by tests/unit/jobs/test_job_run_status_ui.py.

import assert from 'node:assert/strict';

const { RunStatus, runStatus, isActive } = await import('../../app/webapp/static/jobs/run-status.js');

assert.ok(Object.isFrozen(RunStatus));

for (const live of [RunStatus.PENDING, RunStatus.RUNNING]) {
  assert.equal(isActive(live), true, live);
}
for (const done of [RunStatus.QUEUED, RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.SKIPPED,
  RunStatus.DRY_RUN_SUCCESS, RunStatus.DRY_RUN_FAILED]) {
  assert.equal(isActive(done), false, done);
}
for (const unknown of ['', 'Running', 'unconfirmed', null, undefined, 42]) {
  assert.equal(runStatus(unknown), null, String(unknown));
  assert.equal(isActive(unknown), false, String(unknown));
}
assert.equal(runStatus('failed'), RunStatus.FAILED);

console.log('VALUES ' + JSON.stringify(Object.values(RunStatus).sort()));
console.log('OK');
