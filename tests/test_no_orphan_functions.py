"""Static gate: every function under ``src/`` and ``app/`` is named by production code.

A name that appears nowhere outside its own ``def`` is dead; one only ``tests/``
names keeps its tests green around code production never runs. Ruff's F401/F841
see neither. Whole-word search over tracked non-test files, so a string-registered
callback or a JS caller counts as a use; decorated functions and dunders are skipped.
"""

import ast
import re
from collections import Counter
from pathlib import Path

from src.git_utils import run_git

ROOT = Path(__file__).resolve().parent.parent
PROD_SUFFIXES = {".py", ".js", ".mjs", ".html", ".css", ".ps1", ".bat", ".json", ".toml", ".yml", ".mmd"}
ALLOWED_ORPHANS = {  # ``path:name`` -> why production not naming it is fine
    "src/github_client.py:reset_cache": "test seam: empties the module-level cache between tests",
}


def _words(files):
    counts = Counter()
    for name in files:
        counts.update(re.findall(r"[A-Za-z_]\w*", (ROOT / name).read_text(encoding="utf-8")))
    return counts


def _orphans():
    out = run_git(ROOT, ["ls-files", "-z"])
    assert out is not None, "git ls-files failed: the orphan scan is NOT CONFIRMED"
    files = [name for name in out.split("\0") if name]
    prod = _words(f for f in files if not f.startswith("tests/") and Path(f).suffix in PROD_SUFFIXES)
    tests = _words(f for f in files if f.startswith("tests/") and f.endswith(".py"))
    found = {}
    for name in (f for f in files if f.startswith(("src/", "app/")) and f.endswith(".py")):
        for node in ast.walk(ast.parse((ROOT / name).read_text(encoding="utf-8"))):
            is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_def and not node.decorator_list and not node.name.startswith("__") and prod[node.name] <= 1:
                found[f"{name}:{node.name}"] = "test-only" if tests[node.name] else "dead"
    return found


def test_no_function_is_dead_or_named_only_by_tests():
    new = {key: kind for key, kind in _orphans().items() if key not in ALLOWED_ORPHANS}
    assert not new, f"delete these, call them from production, or allow-list with a reason: {new}"


def test_every_allowed_orphan_is_still_an_orphan():
    stale = set(ALLOWED_ORPHANS) - set(_orphans())
    assert not stale, f"drop these from ALLOWED_ORPHANS, they are used or gone: {sorted(stale)}"
