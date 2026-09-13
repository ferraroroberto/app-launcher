"""Exit-code → outcome classification for finished job runs (issue #916).

The defect these pin: the Jobs surface rendered *every* non-zero exit as
``failed``, including the codes fleet-config's scheduled-run adapter reserves
for "this may well have delivered, but nobody established that". Four healthy
weekly jobs showed red for it, and the genuinely failed ones beside them were
camouflaged as a result.

The last test in this module is the anti-drift guard the issue asked for: the
table in :mod:`src.jobs_outcome` is a *reader's copy* of constants that live in
another repo, so it is checked against that repo whenever the sibling checkout
is present rather than trusted to stay correct on its own.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.jobs_outcome import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_UNCONFIRMED,
    classify_exit_code,
    run_outcome,
)


# ------------------------------------------------------- the five in the issue


@pytest.mark.parametrize(
    "exit_code, status, expected",
    [
        # The issue's acceptance criteria, one row each.
        (0, "success", OUTCOME_SUCCESS),
        (123, "failed", OUTCOME_FAILED),   # self-reported: delivered no work
        (120, "failed", OUTCOME_FAILED),   # invoked no tools at all
        (122, "failed", OUTCOME_UNCONFIRMED),  # truncated stream — the bug
        (124, "failed", OUTCOME_FAILED),   # stalled, killed by the watchdog
    ],
)
def test_acceptance_codes(exit_code, status, expected):
    outcome, _ = run_outcome({"status": status, "exit_code": exit_code})
    assert outcome == expected


def test_122_is_not_failed():
    """The whole issue in one assertion."""
    outcome, reason = run_outcome({"status": "failed", "exit_code": 122})
    assert outcome != OUTCOME_FAILED
    assert outcome == OUTCOME_UNCONFIRMED
    assert "never verified" in reason


@pytest.mark.parametrize("exit_code", [124, 123, 120, 125, 119])
def test_real_failures_name_themselves(exit_code):
    """A failure that stays a failure still says *why* — 124 is "stalled",
    not a bare number the reader has to go and look up."""
    outcome, reason = run_outcome({"status": "failed", "exit_code": exit_code})
    assert outcome == OUTCOME_FAILED
    assert reason and reason.strip()


def test_124_names_the_stall():
    _, reason = run_outcome({"status": "failed", "exit_code": 124})
    assert "stalled" in reason


# ------------------------------------------------- the other unconfirmed codes


@pytest.mark.parametrize("exit_code", [114, 118, 121, 122])
def test_every_not_confirmed_code_is_unconfirmed(exit_code):
    """122 is the one that bit, but it is not the only code that means
    "nobody established this". Fixing only the reported one would leave the
    same defect behind at a lower rate."""
    outcome, _ = run_outcome({"status": "failed", "exit_code": exit_code})
    assert outcome == OUTCOME_UNCONFIRMED


def test_118_is_unfinished_work_not_a_failed_delivery():
    """#959: 118 carried 123's "delivered no work" reason under a failed
    outcome, so a run that delivered and then left descendants running paged
    as a failed delivery assertion that never existed."""
    outcome, reason = run_outcome({"status": "failed", "exit_code": 118})
    assert outcome == OUTCOME_UNCONFIRMED
    assert "unfinished" in reason
    assert "delivered no work" not in reason
    assert "delivery assertion" not in reason


def test_123_keeps_the_self_reported_delivered_no_work_reason():
    outcome, reason = run_outcome({"status": "failed", "exit_code": 123})
    assert outcome == OUTCOME_FAILED
    assert "delivered no work" in reason


# --------------------------------------------------------------- the guardrails


def test_unknown_exit_code_keeps_its_status_and_claims_nothing():
    """A child's own exit code (a plain ``1``) is not the adapter's vocabulary.
    It stays failed and carries no invented explanation."""
    outcome, reason = run_outcome({"status": "failed", "exit_code": 1})
    assert outcome == OUTCOME_FAILED
    assert reason is None


def test_reaped_run_with_no_exit_code_stays_failed():
    outcome, reason = run_outcome({"status": "failed", "exit_code": None, "reaped": True})
    assert outcome == OUTCOME_FAILED
    assert reason is None


@pytest.mark.parametrize("field", ["killed", "watchdog", "reaped"])
def test_launcher_terminated_run_is_never_unconfirmed(field):
    """We know exactly what happened to a run this launcher killed, reaped or
    watchdogged: whatever code the torn-down tree reported on its way out is
    not the adapter's verdict, so it can never buy an "unconfirmed"."""
    outcome, _ = run_outcome({"status": "failed", "exit_code": 122, field: True})
    assert outcome == OUTCOME_FAILED


@pytest.mark.parametrize("status", ["running", "pending", "queued", "skipped"])
def test_non_terminal_status_passes_straight_through(status):
    """A run still in flight has no terminal outcome to reinterpret."""
    outcome, _ = run_outcome({"status": status, "exit_code": 122})
    assert outcome == status


def test_success_is_never_reinterpreted():
    outcome, _ = run_outcome({"status": "success", "exit_code": 0})
    assert outcome == OUTCOME_SUCCESS


def test_booleans_are_not_exit_codes():
    """``True`` is an ``int`` in Python and would otherwise classify as 1."""
    assert classify_exit_code(True) is None
    assert classify_exit_code(None) is None
    assert classify_exit_code("122") is None


# ------------------------------------------------------------- the drift guard


def _scheduled_runner() -> Path | None:
    """fleet-config's scheduled-run adapter, if this machine has the checkout.

    Resolved the same way :mod:`src.webapp_config` resolves its default
    ``claude_config_dir`` — the sibling ``fleet-config`` next to this repo — so
    the guard follows the fleet's own layout convention rather than inventing a
    second one. A worktree sits beside the primary checkout, so this resolves
    from either.
    """
    candidate = (
        Path(__file__).resolve().parent.parent.parent
        / "fleet-config"
        / "skills"
        / "_lib"
        / "scheduled_runner.py"
    )
    return candidate if candidate.is_file() else None


def test_exit_code_table_matches_scheduled_runner():
    """Every exit code the adapter defines is one this repo can explain.

    :mod:`src.jobs_outcome` holds a *copy* of a table that is authored in
    another repo, which is exactly the drift the issue warned about: a seventh
    detector added upstream would land here as a silent generic ``failed``,
    with no test going red and nobody noticing until a Board card lied again.
    An importable single source across two repos does not exist (the launcher
    must not depend on fleet-config being installed), so the next best thing is
    to *check* the copy against the original whenever the original is reachable.

    Skips where the sibling checkout is absent — CI, a fresh clone — so this is
    a guard on the dev box that owns both, never a portability trap.
    """
    source = _scheduled_runner()
    if source is None:
        pytest.skip("sibling fleet-config checkout not present")
    text = source.read_text(encoding="utf-8")
    defined = {
        int(match.group(2))
        for match in re.finditer(
            r"^([A-Z_]*EXIT_CODE)\s*=\s*(\d+)\s*$", text, flags=re.MULTILINE
        )
    }
    assert defined, "parsed no *_EXIT_CODE constants — has the adapter moved?"
    unexplained = {code for code in defined if classify_exit_code(code) is None}
    assert not unexplained, (
        "scheduled_runner.py defines exit code(s) src.jobs_outcome does not "
        f"name: {sorted(unexplained)}. Classify each one (failed vs "
        "unconfirmed) rather than letting it render as a generic failure."
    )


# Codes whose verdict rung in ``ProgressFormatter.finish`` keys on a formatter
# flag rather than on the exit code itself (or that are decided outside
# ``finish`` altogether), so no glyph can be read off a code comparison. Keyed
# by constant *name*, so a renumbered or newly added constant still has to be
# classified here or picked up by the parsed rungs.
_FLAG_KEYED_OUTCOMES = {
    "STALL_EXIT_CODE": OUTCOME_FAILED,                    # ⏱ stalled
    "BACKGROUND_KILL_EXIT_CODE": OUTCOME_FAILED,          # ❌ background tasks killed
    "SELF_REPORTED_FAILURE_EXIT_CODE": OUTCOME_FAILED,    # ❌ delivered no work
    "DELIVERY_NOT_CONFIRMED_EXIT_CODE": OUTCOME_UNCONFIRMED,  # ❓ final outcome
}

_GLYPH_OUTCOMES = {"❓": OUTCOME_UNCONFIRMED, "❌": OUTCOME_FAILED, "⏱": OUTCOME_FAILED}


def _code_keyed_verdicts(text: str) -> dict:
    """``{constant name: outcome}`` for every rung of ``ProgressFormatter.finish``
    whose condition compares ``exit_code`` against a ``*_EXIT_CODE`` constant.

    The outcome is read off the leading glyph of the ``status`` string that
    rung assigns — the same line the job's own ``output.log`` ends with — so the
    guard pins what each code *means*, not merely that it exists (#959: 118
    sat in the table as ``failed`` under 123's wording while the adapter
    printed ``❓ not confirmed`` beside it).
    """
    tree = ast.parse(text)
    finish = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "ProgressFormatter"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "finish"
    )

    def status_glyph(body):
        for stmt in body:
            if not (isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "status" for t in stmt.targets
            )):
                continue
            value = stmt.value
            if isinstance(value, ast.JoinedStr):
                value = value.values[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value[:1]
        return None

    verdicts = {}
    for node in ast.walk(finish):
        if not isinstance(node, ast.If):
            continue
        compares_code = any(
            isinstance(sub, ast.Compare)
            and isinstance(sub.left, ast.Name)
            and sub.left.id == "exit_code"
            for sub in ast.walk(node.test)
        )
        if not compares_code:
            continue
        names = {
            sub.id
            for sub in ast.walk(node.test)
            if isinstance(sub, ast.Name) and sub.id.endswith("_EXIT_CODE")
        }
        glyph = status_glyph(node.body)
        for name in names:
            verdicts[name] = _GLYPH_OUTCOMES.get(glyph)
    return verdicts


def test_exit_code_outcomes_match_scheduled_runner_verdicts():
    """Every adapter code's outcome class agrees with the verdict the adapter
    prints for it — ``❓`` → unconfirmed, ``❌``/``⏱`` → failed.

    Presence alone (the guard above) let 118 carry a failure class and another
    code's reason for a week. Skips where the sibling checkout is absent.
    """
    source = _scheduled_runner()
    if source is None:
        pytest.skip("sibling fleet-config checkout not present")
    text = source.read_text(encoding="utf-8")
    constants = {
        match.group(1): int(match.group(2))
        for match in re.finditer(
            r"^([A-Z_]*EXIT_CODE)\s*=\s*(\d+)\s*$", text, flags=re.MULTILINE
        )
    }
    parsed = _code_keyed_verdicts(text)
    assert parsed, "parsed no exit-code rungs in ProgressFormatter.finish"
    assert None not in parsed.values(), (
        f"a code-keyed rung assigns a status with no known glyph: {parsed}"
    )

    expected = {**_FLAG_KEYED_OUTCOMES, **parsed}
    unclassified = sorted(set(constants) - set(expected))
    assert not unclassified, (
        f"no verdict rung or _FLAG_KEYED_OUTCOMES entry for {unclassified} — "
        "read its verdict in scheduled_runner.py and classify it here"
    )
    overlap = {
        name for name in set(parsed) & set(_FLAG_KEYED_OUTCOMES)
        if parsed[name] != _FLAG_KEYED_OUTCOMES[name]
    }
    assert not overlap, f"_FLAG_KEYED_OUTCOMES contradicts the parsed rung for {overlap}"

    mismatched = []
    for name, code in sorted(constants.items(), key=lambda item: item[1]):
        got = (classify_exit_code(code) or (None,))[0]
        if got != expected[name]:
            mismatched.append(f"{name}={code}: table says {got}, adapter says {expected[name]}")
    assert not mismatched, (
        "src.jobs_outcome disagrees with the adapter's own verdict: "
        + "; ".join(mismatched)
    )
