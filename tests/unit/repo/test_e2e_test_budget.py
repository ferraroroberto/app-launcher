"""The e2e suite stays at its declared ``[e2e] test_budget``.

``/e2e``'s budget step only reports an over-budget suite; this is the check that
fails the build. The budget is a ratchet, so it must equal the collected node
count: more nodes than declared means the suite regrew, fewer means the number
is stale headroom that a later addition could hide in. Either way the fix is in
``.fleet.toml``, and raising it needs a written reason beside the number.

The count is what a full-tier gate run selects: ``pytest --collect-only`` over
``tests/e2e`` after ``tests/e2e/conftest.py`` has deselected the WebKit nodes
that do not carry ``iphone``. A collection that fails, or whose summary line
cannot be read, is a failure, never a pass.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from src.subprocess_flags import NO_WINDOW

_REPO = Path(__file__).resolve().parents[3]
# "645/852 tests collected (207 deselected)": the first number is what a run selects.
_COLLECTED = re.compile(r"^(\d+)(?:/\d+)? tests? collected", re.MULTILINE)


def _declared_budget() -> int:
    cfg = tomllib.loads((_REPO / ".fleet.toml").read_text(encoding="utf-8"))
    budget = cfg["e2e"]["test_budget"]
    assert isinstance(budget, int) and budget > 0, f"[e2e] test_budget must be a positive integer, got {budget!r}"
    return budget


def _collected_nodes() -> int:
    # An inherited xdist worker id would make the child act as a worker.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")}
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests/e2e"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=110,
        creationflags=NO_WINDOW,
    )
    match = _COLLECTED.search(proc.stdout)
    assert proc.returncode == 0 and match, (
        f"e2e collection did not establish a node count (exit {proc.returncode}):\n"
        f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
    )
    return int(match.group(1))


def test_e2e_node_count_equals_the_declared_budget() -> None:
    budget, nodes = _declared_budget(), _collected_nodes()
    assert nodes <= budget, (
        f"tests/e2e collects {nodes} nodes, over the [e2e] test_budget of {budget} in .fleet.toml. "
        "Fold the new test onto an existing same-setup one; raise the budget only with a written reason beside it."
    )
    assert nodes >= budget, (
        f"tests/e2e collects {nodes} nodes, under the [e2e] test_budget of {budget}. "
        f"Lower test_budget to {nodes} in .fleet.toml so the freed headroom cannot be spent."
    )
