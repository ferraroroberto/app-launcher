# Job run state

A job run's state is the `status` field of its `run.json`. Several processes write it (the webapp's admission and Kill routes, the detached executor, the stranded-run reaper, the mutex-queue drain) and every Jobs surface reads it, so the closed set of values and the groupings readers need are declared once, in `src/jobs/run_status.py`. It is declared in `.fleet.toml` as the `job-run-status` state machine, so the conventions ratchet's state rules report any other module that spells these states as raw strings.

## States

| State | Meaning | Written by |
| --- | --- | --- |
| `queued` | Waiting in its mutex group's FIFO behind a running holder | admission (`src/jobs/queue.py`) |
| `pending` | Admitted and spawned; the executor has not started the child yet | admission, the dry-run route's seed |
| `running` | The executor's child is running | executor |
| `success` | Finished with a zero exit code | executor |
| `failed` | Finished any other way: non-zero exit, killed, reaped, refused, spawn error | executor, Kill route, reaper, admission |
| `skipped` | A fire the executor's cooldown gate declined; nothing was spawned | executor |
| `dry_run_success` | A dry-run check resolved the invocation; nothing was spawned | dry-run route |
| `dry_run_failed` | A dry-run check could not resolve the invocation; the reason is in `note` | dry-run route |

Transitions: `queued` to `pending` (the drain spawns the head) or `failed`; `pending` to `running` or `failed`; `running` to `success` or `failed`. `success` and `failed` are terminal. The three no-spawn states are written once, already terminal.

The finer outcome of a finished run (`unconfirmed`, `deferred`) is derived from the exit code at read time by `src/jobs/outcome.py`; it is not a stored state.

## Groupings

- `ACTIVE` (`pending`, `running`): holds its mutex group and may still be killed or reaped. `is_active(record)` asks it.
- `FINISHED` (`success`, `failed`): counts toward the success ratio and the duration percentiles.
- `NOT_A_FIRE` (`skipped`, `dry_run_success`, `dry_run_failed`): never a cooldown anchor.

## Readers

A reader calls `run_status(record)` (a value outside the set, or no record, is `None`) or `is_active(record)`, and writers store a `RunStatus` member, which serialises as its plain string. A new reader never compares the raw `status` string.

The terminal rule is enforced where two writers race: the reaper's guarded write re-checks `is_active` under the run lock and refuses to overwrite a run the executor already finished. `tests/unit/jobs/test_job_run_status.py` holds the state table and that refusal.

## The Jobs tab

The browser reads the same values from `app/webapp/static/jobs/run-status.js`: `RunStatus` (the same eight values), `runStatus(status)` (`null` outside the set) and `isActive(status)`. The tab streams live output and offers Kill only while `isActive` holds, so a finished or unknown run never gets either. It is declared as the `job-run-status-ui` state machine; `tests/unit/jobs/test_job_run_status_ui.py` runs its Node pin and fails when its values drift from `RunStatus`.
