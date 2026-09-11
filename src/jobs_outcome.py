"""Terminal-outcome classification for a finished job run (issue #916).

A job's persisted ``status`` is binary — ``"success"`` for exit 0, ``"failed"``
for anything else (``app.cli.commands.run_job_cmd._spawn_and_wait``). That was
fine while a job's child was an ordinary script whose non-zero exit meant "this
broke". It stopped being fine once most jobs became scheduled Claude runs driven
by fleet-config's ``skills/_lib/scheduled_runner.py``, which spends a whole
block of exit codes distinguishing *why* a run did not report success — and
three of those codes do not mean failure at all. They mean **the adapter could
not establish whether the run delivered**, which is a different fact and the one
the global rule insists must be its own state rather than folded into a
neighbour.

Rendering those as ``failed`` produced the false alarms in #916: healthy runs
that had committed, pushed and posted to Telegram showed red, and the one
genuinely failed job among them was camouflaged by the fakes.

So this module adds a third outcome, ``unconfirmed``, derived from the exit code
at read time. Derived, not persisted: historical run records — the ones already
on disk showing red — re-render correctly without a migration, and ``status``
keeps meaning exactly what it has always meant for every consumer that has not
been taught the new vocabulary.

**Provenance of the table below.** These codes are *defined* by
``scheduled_runner.py``; this is a reader's copy, which is why
``tests/test_jobs_outcome.py::test_exit_code_table_matches_scheduled_runner``
parses that file (when the sibling fleet-config checkout is present) and fails
if it grows a code this table does not name. A new upstream code would otherwise
land here silently as a generic failure — the drift the issue asked to prevent.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

#: A run the adapter confirmed delivered.
OUTCOME_SUCCESS = "success"
#: A run that demonstrably did not deliver.
OUTCOME_FAILED = "failed"
#: A run whose delivery nobody established — it may well have succeeded.
OUTCOME_UNCONFIRMED = "unconfirmed"

# exit code -> (outcome, one-line reason). Wording condensed from the verdict
# strings ``ProgressFormatter.finish`` prints beside each code, so a job card
# and its own log agree. Codes absent here (a child's own exit code, e.g. a
# plain `1`) carry no reason and stay whatever ``status`` said.
_EXIT_CODES: Dict[int, Tuple[str, str]] = {
    0: (OUTCOME_SUCCESS, ""),
    2: (OUTCOME_FAILED, "the scheduled-run adapter rejected its own arguments"),
    114: (
        OUTCOME_UNCONFIRMED,
        "cancellation not confirmed — owned descendants could not be verified",
    ),
    115: (OUTCOME_FAILED, "required tools were unavailable"),
    116: (OUTCOME_FAILED, "the model was unavailable"),
    117: (OUTCOME_FAILED, "authentication was unavailable"),
    118: (
        OUTCOME_FAILED,
        "the run reported it delivered no work — see its final report for "
        "which delivery assertion failed",
    ),
    119: (
        OUTCOME_FAILED,
        "transient upstream API error (5xx) — the run never got going; an "
        "API-side fault, not a fault in the job",
    ),
    120: (
        OUTCOME_FAILED,
        "the run invoked no tools at all — the skill never started",
    ),
    121: (
        OUTCOME_UNCONFIRMED,
        "the delivery check could not confirm this run delivered anything",
    ),
    122: (
        OUTCOME_UNCONFIRMED,
        "the completion stream was truncated — the run was cut off mid-flight "
        "and delivery was never verified",
    ),
    123: (
        OUTCOME_FAILED,
        "the run printed its own failure marker",
    ),
    124: (
        OUTCOME_FAILED,
        "stalled — no stream activity for long enough that the watchdog "
        "killed it",
    ),
    125: (
        OUTCOME_FAILED,
        "background tasks were killed after timeout — the run likely ended "
        "its turn with agents still in flight",
    ),
    127: (OUTCOME_FAILED, "the agent failed to start"),
    130: (OUTCOME_FAILED, "cancelled — the owned process tree was stopped"),
}

# A run this launcher itself ended or found dead. Whatever exit code such a
# record carries was produced by a torn-down process, not by the adapter's
# verdict chain, so it can never be read as "not confirmed" — we know exactly
# what happened to it, and it is not success.
_LAUNCHER_TERMINATED_FIELDS = ("killed", "watchdog", "reaped")


def classify_exit_code(exit_code: Optional[int]) -> Optional[Tuple[str, str]]:
    """``(outcome, reason)`` for a known adapter exit code, else ``None``.

    ``None`` for an unrecognised code (including ``None`` itself, which is what
    a reaped run carries) — the caller keeps whatever ``status`` already said
    rather than inventing a verdict for a code nobody defined.
    """
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    return _EXIT_CODES.get(exit_code)


def run_outcome(record: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(outcome, reason)`` for one run record.

    ``outcome`` is a superset of ``status``: it equals ``status`` for every
    record except a ``failed`` one whose exit code says the adapter never
    established delivery, which becomes :data:`OUTCOME_UNCONFIRMED`. Non-terminal
    statuses (``running`` / ``pending`` / ``queued``) pass straight through, so
    a run in flight is never re-labelled.

    ``reason`` is the exit code's one-liner whenever the code is a known one —
    including for codes that stay ``failed``, because "stalled" and "invoked no
    tools" are worth naming on the card rather than leaving to a log dive.
    """
    status = record.get("status")
    classified = classify_exit_code(record.get("exit_code"))
    if classified is None:
        return status, None
    outcome, reason = classified
    if status != OUTCOME_FAILED:
        # Only a "failed" verdict is reinterpretable. A success stays a
        # success, and a run still in flight has no terminal outcome yet.
        return status, reason or None
    if any(record.get(field) for field in _LAUNCHER_TERMINATED_FIELDS):
        return OUTCOME_FAILED, reason or None
    if outcome == OUTCOME_UNCONFIRMED:
        return OUTCOME_UNCONFIRMED, reason or None
    return OUTCOME_FAILED, reason or None
