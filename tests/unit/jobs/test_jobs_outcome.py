"""Exit-code → outcome classification for finished job runs (issue #916).

The defect these pin: the Jobs surface rendered *every* non-zero exit as
``failed``, including the codes fleet-config's scheduled-run adapter reserves
for "this may well have delivered, but nobody established that". Four healthy
weekly jobs showed red for it, and the genuinely failed ones beside them were
camouflaged as a result.

The last test in this module is the anti-drift guard the issue asked for: the
table in :mod:`src.jobs.outcome` is a *reader's copy* of constants that live in
another repo, so it is checked against that repo whenever the sibling checkout
is present rather than trusted to stay correct on its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.jobs.outcome import (
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


def _run_outcome_module() -> Path | None:
    """fleet-config's scheduled-run outcome module, if this machine has the checkout.

    ``scheduled/run_outcome.py`` declares every way a scheduled run ends as one
    ``RunState`` enum (value: exit code, meaning) and the verdict headline each
    state prints (``_HEADLINE``; a state left out prints ``❌ failed``).

    Resolved the same way :mod:`src.webapp_config` resolves its default
    ``fleet_config_dir`` — the sibling ``fleet-config`` next to this repo — so
    the guard follows the fleet's own layout convention rather than inventing a
    second one. A worktree sits beside the primary checkout, so this resolves
    from either.
    """
    candidate = (
        Path(__file__).resolve().parent.parent.parent.parent.parent
        / "fleet-config"
        / "skills"
        / "_lib"
        / "scheduled"
        / "run_outcome.py"
    )
    return candidate if candidate.is_file() else None


#: ``RunState.FAILED`` keeps the child's own non-zero exit code, so it names no
#: reason of its own; src.jobs.outcome leaves such codes to ``status``.
_CHILD_OWN_CODE_STATES = {"FAILED"}

_GLYPH_OUTCOMES = {
    "✅": OUTCOME_SUCCESS,
    "❓": OUTCOME_UNCONFIRMED,
    "❌": OUTCOME_FAILED,
    "⏱": OUTCOME_FAILED,
}
#: The headline ``status_line`` prints for a state ``_HEADLINE`` leaves out.
_DEFAULT_GLYPH = "❌"


def _run_states(tree: ast.Module) -> dict:
    """``{RunState member name: exit code}`` read from the enum's value tuples."""
    enum_cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RunState"
    )
    states = {}
    for stmt in enum_cls.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Tuple)
            and isinstance(stmt.value.elts[0], ast.Constant)
            and isinstance(stmt.value.elts[0].value, int)
        ):
            states[stmt.targets[0].id] = stmt.value.elts[0].value
    return states


def _headline_glyphs(tree: ast.Module) -> dict:
    """``{RunState member name: leading glyph}`` for every ``_HEADLINE`` entry."""
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "_HEADLINE" for t in targets):
            assert isinstance(node.value, ast.Dict), "_HEADLINE is no longer a dict literal"
            return {
                key.attr: value.value[:1]
                for key, value in zip(node.value.keys, node.value.values)
                if isinstance(key, ast.Attribute) and isinstance(value, ast.Constant)
            }
    raise AssertionError("run_outcome.py no longer defines _HEADLINE")


def test_exit_code_table_matches_scheduled_runner():
    """Every exit code the adapter defines is one this repo can explain.

    :mod:`src.jobs.outcome` holds a *copy* of a table that is authored in
    another repo, which is exactly the drift the issue warned about: a new
    state added upstream would land here as a silent generic ``failed``, with
    no test going red and nobody noticing until a Board card lied again.
    An importable single source across two repos does not exist (the launcher
    must not depend on fleet-config being installed), so the next best thing is
    to *check* the copy against the original whenever the original is reachable.

    Skips where the sibling checkout is absent — CI, a fresh clone — so this is
    a guard on the dev box that owns both, never a portability trap.
    """
    source = _run_outcome_module()
    if source is None:
        pytest.skip("sibling fleet-config checkout not present")
    states = _run_states(ast.parse(source.read_text(encoding="utf-8")))
    assert states, "parsed no RunState members — has the adapter moved?"
    unexplained = {
        f"{name}={code}" for name, code in states.items()
        if name not in _CHILD_OWN_CODE_STATES and classify_exit_code(code) is None
    }
    assert not unexplained, (
        "scheduled/run_outcome.py defines exit code(s) src.jobs.outcome does not "
        f"name: {sorted(unexplained)}. Classify each one (failed vs "
        "unconfirmed) rather than letting it render as a generic failure."
    )


def test_exit_code_outcomes_match_scheduled_runner_verdicts():
    """Every adapter code's outcome class agrees with the verdict the adapter
    prints for it — ``✅`` → success, ``❓`` → unconfirmed, ``❌``/``⏱`` → failed.

    Presence alone (the guard above) let 118 carry a failure class and another
    code's reason for a week. Skips where the sibling checkout is absent.
    """
    source = _run_outcome_module()
    if source is None:
        pytest.skip("sibling fleet-config checkout not present")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    states = _run_states(tree)
    glyphs = _headline_glyphs(tree)
    assert glyphs, "parsed no _HEADLINE entries in run_outcome.py"
    unknown = {name: g for name, g in glyphs.items() if g not in _GLYPH_OUTCOMES}
    assert not unknown, f"a _HEADLINE entry starts with no known glyph: {unknown}"

    mismatched = []
    for name, code in sorted(states.items(), key=lambda item: item[1]):
        if name in _CHILD_OWN_CODE_STATES:
            continue
        expected = _GLYPH_OUTCOMES[glyphs.get(name, _DEFAULT_GLYPH)]
        got = (classify_exit_code(code) or (None,))[0]
        if got != expected:
            mismatched.append(f"{name}={code}: table says {got}, adapter says {expected}")
    assert not mismatched, (
        "src.jobs.outcome disagrees with the adapter's own verdict: "
        + "; ".join(mismatched)
    )
