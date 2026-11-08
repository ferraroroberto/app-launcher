"""One retry for e2e tests that failed the way a loaded box fails.

The pre-ship gate loads this plugin with ``-p tests._load_retry`` and points
``LAUNCHER_E2E_FAILURES_FILE`` at a file under ``webapp/``. Every failed
setup, call or teardown report is appended to it as one JSON line: the node
id and whether its failure text carries a load signature.

A load signature is an operation that ran out of time or socket resources: a
Playwright action, navigation or ``expect()`` wait that timed out, a
``requests`` read or connect timeout, a bare socket ``timed out``, or
Chromium's ``ERR_NO_BUFFER_SPACE`` / ``ERR_ADDRESS_IN_USE`` /
``ERR_TIMED_OUT``. On this host the whole box stalls for 25-30 s at a time
when other lanes run: every worker's own disposable webapp misses its 15 s
``goto`` and 30 s read budget in the same second, and a CPU-heavy render (a
Mermaid diagram) misses a 5 s ``expect()``. A plain Python assertion, a
refused connection or a crashed worker is not a load signature, so it stays
red. A real bug that only shows as a timeout fails its one retry too, and the
second red is red.

``python -m tests._load_retry plan <file>`` reads the file after a red pass:

- exit 0, one ``RETRY=<nodeid>`` line per failed test: every failure is
  load-shaped and there are at most ``MAX_RETRY`` of them. The gate re-runs
  exactly those, once; a pass there is recorded as flaky, a second red is red.
- exit 1, ``NO-RETRY=<reason>``: a failure that is not load-shaped, or too
  many failures to be a stall.
- exit 3, ``NO-RETRY=<reason>``: the file is missing, unreadable or empty, so
  which tests failed is not established. Never a retry.

Under pytest-xdist only the controller writes: it receives every worker's
reports, so the file has one writer. Inert when the env var is absent.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

FAILURES_ENV = "LAUNCHER_E2E_FAILURES_FILE"
# More failures than this in one pass is a broken build or a dead server, not
# a stall: the observed stalls failed one to four tests across three workers.
MAX_RETRY = 8

_LOAD_SIGNATURES = re.compile(
    r"TimeoutError: \w+\.\w+: Timeout \d+ms exceeded"
    r"|Expect \"\w+\"[^\n]* with timeout \d+ms"
    r"|requests\.exceptions\.(?:ReadTimeout|ConnectTimeout)"
    r"|TimeoutError: timed out"
    r"|net::ERR_(?:NO_BUFFER_SPACE|ADDRESS_IN_USE|TIMED_OUT)\b"
)


def is_load_shaped(failure_text: str) -> bool:
    """True when a failure's text names a timeout or socket-resource error."""
    return bool(_LOAD_SIGNATURES.search(failure_text))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    path = os.environ.get(FAILURES_ENV, "").strip()
    if not path or os.environ.get("PYTEST_XDIST_WORKER") or not report.failed:
        return
    line = json.dumps({
        "nodeid": report.nodeid,
        "when": report.when,
        "load": is_load_shaped(report.longreprtext),
    })
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def plan(path: Path) -> Tuple[int, List[str]]:
    """The exit code and output lines of ``plan`` for one failures file."""
    try:
        rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, ValueError) as exc:
        return 3, [f"NO-RETRY=failures file not readable: {exc}"]
    if not rows:
        return 3, ["NO-RETRY=no failure recorded, so which tests failed is not established"]
    not_load = sorted({r["nodeid"] for r in rows if not r["load"]})
    if not_load:
        return 1, [f"NO-RETRY=not a timeout or socket error: {nodeid}" for nodeid in not_load]
    nodeids = list(dict.fromkeys(r["nodeid"] for r in rows))
    if len(nodeids) > MAX_RETRY:
        return 1, [f"NO-RETRY={len(nodeids)} failures, more than {MAX_RETRY}: not a stall"]
    return 0, [f"RETRY={nodeid}" for nodeid in nodeids]


def main(argv: List[str]) -> int:
    if len(argv) != 2 or argv[0] != "plan":
        print("usage: python -m tests._load_retry plan <failures-file>", file=sys.stderr)
        return 2
    code, lines = plan(Path(argv[1]))
    sys.stdout.write("".join(f"{ln}\n" for ln in lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
