"""The launcher's request budget table and its owner-tunable overrides.

The timing machinery (executors, verdicts, in-flight peaks) is the scaffold's
vendored ``src/request_timing.py`` and is tested there; this holds what is the
launcher's own: which requests fall in which class, and what the defaults are.
"""

from __future__ import annotations

import pytest

from src.request_health import BUDGETS, class_names, clean_overrides
from src.request_timing import Budget, classify, resolve_budgets


@pytest.mark.parametrize(
    ("method", "path", "query", "expected"),
    [
        ("GET", "/api/board", "", "board"),
        ("GET", "/api/claude-code/sessions", "", "session-list"),
        ("GET", "/api/claude-code/sessions/abc/transcript", "after=10&size=10", "transcript-tick"),
        ("GET", "/api/claude-code/sessions/abc/transcript", "limit=20", "transcript-open"),
        ("POST", "/api/claude-code/sessions/abc/input", "", "send"),
        ("GET", "/api/claude-code/git-status", "fresh=true", "git-status"),
        ("GET", "/", "", "shell"),
        ("GET", "/api/jobs", "", "jobs-list"),
        ("POST", "/api/board", "", None),
        ("GET", "/api/version", "", None),
    ],
)
def test_classify(method, path, query, expected):
    assert classify(BUDGETS, method, path, query) == expected


def test_the_issue_budgets_are_the_defaults():
    table = resolve_budgets(BUDGETS)
    assert table["transcript-tick"] == Budget(0.5, 1.5)
    assert table["session-list"] == Budget(0.5, 1.5)
    assert table["board"] == Budget(0.5, 1.5)
    assert table["send"] == Budget(5.0, 10.0)
    assert table["git-status"] == Budget(5.0, 10.0)
    assert table["transcript-open"].soft_s == 1.0
    assert list(table) == class_names()


def test_overrides_replace_a_class_and_bad_ones_are_dropped():
    kept, dropped = clean_overrides(
        {"board": [1, 2], "nope": [1, 2], "send": [3, 1], "git-status": "x"}
    )
    assert kept == {"board": [1.0, 2.0]}
    assert len(dropped) == 3
    table = resolve_budgets(BUDGETS, {"board": [1, 2], "send": [9, 1]})
    assert table["board"] == Budget(1.0, 2.0)
    assert table["send"] == Budget(5.0, 10.0)  # an invalid pair never switches a budget off


def test_overrides_that_are_not_an_object_are_dropped_whole():
    assert clean_overrides(None) == ({}, [])
    kept, dropped = clean_overrides([1, 2])
    assert kept == {} and dropped
