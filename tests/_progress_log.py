"""Gate progress log (issues #534, #943).

When the pre-ship gate (scripts/verify-before-ship.ps1) sets
LAUNCHER_VERIFY_PROGRESS_LOG, every test's start and finish is appended — and
flushed — to that file as it happens. A gate that wedges or is killed by an
outer timeout then leaves the ACTIVE node id (last START without a DONE),
per-test totals (setup+call+teardown, so fixture cost is visible), and a
slowest-tests summary on disk, instead of an opaque dead console.

A failing setup or call also leaves a bounded excerpt of its traceback under
the ``FAILED`` line (#943), so a red is diagnosable from disk after the
console that ran the gate is gone (a reviewer sub-agent, a killed run, a
chief-dispatched worker). Excerpt lines carry a ``    | `` prefix instead of
the timestamp, so ``grep -v '^    |'`` still yields the one-line-per-event log.

The excerpt is read from ``report.longreprtext`` in ``pytest_runtest_logreport``,
which pytest fires only after ``pytest_runtest_makereport`` has returned — by
then ``tests._credential_hygiene`` has already scrubbed the report, so the
excerpt inherits its redaction whatever order the two plugins registered in.

Parallel workers (#1231): under pytest-xdist only the controller writes. It
already receives every worker's start and report hooks, so the log stays one
writer with no interleaved half-lines, and each DONE / failure line names the
worker (``[gw2]``) that ran it. Several tests are in flight at once, so a
wedged run is diagnosed by *every* START without a matching DONE, not only
the last START line.

Inert for normal pytest runs (env var absent → every hook no-ops). Wired by
re-export from ``tests/conftest.py``; loadable on its own with
``-p tests._progress_log``.
"""

from __future__ import annotations

import os
import time
from typing import Iterable, List

import pytest

PROGRESS_ENV = "LAUNCHER_VERIFY_PROGRESS_LOG"
EXCERPT_PREFIX = "    | "
# Bounds, so a 360-node run with many reds cannot balloon the log: the tail of
# each traceback (where the crash line and its `E` lines sit), each line capped,
# and excerpts only for the first failures of a run — every later red still
# gets its FAILED line. Worst case is roughly 30 × 41 × 250 bytes ≈ 310 KB.
EXCERPT_MAX_LINES = 40
EXCERPT_MAX_LINE_CHARS = 240
EXCERPT_MAX_FAILURES = 30

_t0 = time.monotonic()
_node_totals: dict = {}
_durations: list = []
_excerpts_written = 0


def _write(line: str, detail: Iterable[str] = ()) -> None:
    path = os.environ.get(PROGRESS_ENV, "").strip()
    # An xdist worker leaves the log to the controller, which sees its hooks.
    if not path or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    stamp = time.strftime("%H:%M:%S")
    elapsed = time.monotonic() - _t0
    block = f"[{stamp} +{elapsed:7.1f}s] {line}\n"
    block += "".join(f"{EXCERPT_PREFIX}{d}\n" for d in detail)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError:  # never let diagnostics fail the run
        pass


def failure_excerpt(report: pytest.TestReport) -> List[str]:
    """The last ``EXCERPT_MAX_LINES`` non-blank lines of a report's failure text."""
    lines = [ln.rstrip() for ln in report.longreprtext.splitlines() if ln.strip()]
    omitted = len(lines) - EXCERPT_MAX_LINES
    if omitted > 0:
        lines = [f"... {omitted} earlier line(s) omitted"] + lines[-EXCERPT_MAX_LINES:]
    return [
        ln if len(ln) <= EXCERPT_MAX_LINE_CHARS else ln[:EXCERPT_MAX_LINE_CHARS] + " ..."
        for ln in lines
    ]


def _worker_tag(report: pytest.TestReport) -> str:
    """`` [gw2]`` for a report the xdist controller received, else ``""``."""
    gateway = getattr(getattr(report, "node", None), "gateway", None)
    worker = getattr(gateway, "id", None)
    return f" [{worker}]" if worker else ""


def pytest_runtest_logstart(nodeid, location) -> None:
    _write(f"START {nodeid}")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _excerpts_written
    total = _node_totals.get(report.nodeid, 0.0) + report.duration
    _node_totals[report.nodeid] = total
    if report.when in ("setup", "call") and report.outcome != "passed":
        # skipped / failed / errored — name the phase so a fixture skip is
        # distinguishable from an assertion failure.
        line = f"{report.outcome.upper()} ({report.when}) {report.nodeid}{_worker_tag(report)}"
        detail: List[str] = []
        if report.failed and os.environ.get(PROGRESS_ENV, "").strip():
            _excerpts_written += 1
            if _excerpts_written <= EXCERPT_MAX_FAILURES:
                detail = failure_excerpt(report)
            elif _excerpts_written == EXCERPT_MAX_FAILURES + 1:
                detail = [
                    f"(traceback excerpts stop after {EXCERPT_MAX_FAILURES} failures;"
                    " later ones are in the console output only)"
                ]
        _write(line, detail)
    if report.when == "teardown":
        _node_totals.pop(report.nodeid, None)
        _durations.append((total, report.nodeid))
        _write(f"DONE  {report.nodeid} ({total:.1f}s){_worker_tag(report)}")


def write_session_summary(exitstatus: int) -> None:
    """Append the session's exit status and slowest-15 tests to the log."""
    if not os.environ.get(PROGRESS_ENV, "").strip():
        return
    _write(f"pytest session finished (exit status {exitstatus})")
    slowest = sorted(_durations, key=lambda t: t[0], reverse=True)[:15]
    if slowest:
        _write("slowest tests (setup+call+teardown):")
        for duration, nodeid in slowest:
            _write(f"  {duration:7.1f}s  {nodeid}")
