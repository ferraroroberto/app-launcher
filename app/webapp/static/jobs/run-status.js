/* A job run's lifecycle status, as the Jobs tab reads it.
 *
 * The server declares the closed set once, in src/jobs/run_status.py
 * (docs/job-run-state.md has the lifecycle table); a run record's `status`
 * and the run-now response's `status` both carry these values. This module
 * is the browser's copy of that set, so the tab compares against RunStatus
 * members and asks isActive() instead of re-spelling the strings.
 * tests/unit/jobs/test_job_run_status_ui.py fails when the two sets drift.
 */

export const RunStatus = Object.freeze({
  QUEUED: 'queued',
  PENDING: 'pending',
  RUNNING: 'running',
  SUCCESS: 'success',
  FAILED: 'failed',
  SKIPPED: 'skipped',
  DRY_RUN_SUCCESS: 'dry_run_success',
  DRY_RUN_FAILED: 'dry_run_failed',
});

const KNOWN = new Set(Object.values(RunStatus));
// Not finished yet: the run still streams output and may be killed.
const ACTIVE = new Set([RunStatus.PENDING, RunStatus.RUNNING]);

// The RunStatus member a raw status string is, or null for anything outside the set.
export function runStatus(status) {
  return KNOWN.has(status) ? status : null;
}

// True while the run is pending or running; every other value, unknown ones included, is not live.
export function isActive(status) {
  return ACTIVE.has(runStatus(status));
}
