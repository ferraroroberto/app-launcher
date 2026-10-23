"""Static gate: every function under ``src/`` and ``app/`` is named by production code.

A function whose name appears nowhere outside its own ``def`` is dead, and one
named only by ``tests/`` is worse: its tests stay green around code production
never runs. Ruff's F401/F841 cannot see either (they stop at unused imports and
locals), so the audit's recurring "stale" bucket regrew by hand each week.

The check is a whole-word search over every tracked non-test, non-Markdown file
(Python, JS, HTML, scripts, config), so a callback registered by string, a JS
caller of a route, or a ``getattr`` name all count as a use. Decorated functions
(routes, properties, fixtures) and dunders are skipped: something else
registers them. A name shared with another symbol counts as used, so the check
under-reports rather than flags live code.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Set, Tuple

from src.git_utils import run_git

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src/", "app/")
PROD_SUFFIXES = {
    ".py", ".js", ".mjs", ".html", ".css", ".ps1", ".bat", ".json", ".toml",
    ".yml", ".yaml", ".txt", ".mmd",
}
#: ``path:name`` -> why a function nothing in production names is kept.
ALLOWED_ORPHANS: Dict[str, str] = {
    "src/github_client.py:reset_cache": "test seam: the only way to empty the module-level cache between tests",
}


def _tracked_files() -> List[str]:
    out = run_git(PROJECT_ROOT, ["ls-files", "-z"])
    assert out is not None, "git ls-files failed: the orphan scan is NOT CONFIRMED"
    return [name for name in out.split("\0") if name]


def _word_counts(files: List[str]) -> Counter:
    counts: Counter = Counter()
    for name in files:
        counts.update(re.findall(r"[A-Za-z_]\w*", (PROJECT_ROOT / name).read_text(encoding="utf-8")))
    return counts


def _defined(files: List[str]) -> List[Tuple[str, int, str]]:
    found = []
    for name in files:
        if not (name.startswith(SCAN_DIRS) and name.endswith(".py")):
            continue
        for node in ast.walk(ast.parse((PROJECT_ROOT / name).read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.decorator_list and not node.name.startswith("__"):
                    found.append((name, node.lineno, node.name))
    return found


def _orphans() -> Tuple[Set[str], Dict[str, str]]:
    """``(orphan keys, key -> "test-only" | "dead")`` for the tracked tree."""
    files = _tracked_files()
    prod = _word_counts([
        f for f in files
        if not f.startswith("tests/") and Path(f).suffix in PROD_SUFFIXES
    ])
    tests = _word_counts([f for f in files if f.startswith("tests/") and f.endswith(".py")])
    kinds = {
        f"{path}:{name}": "test-only" if tests[name] else "dead"
        for path, _, name in _defined(files)
        if prod[name] <= 1  # the def itself
    }
    return set(kinds), kinds


def test_no_function_is_dead_or_named_only_by_tests():
    orphans, kinds = _orphans()
    new = sorted(orphans - set(ALLOWED_ORPHANS))
    assert not new, (
        "functions no production file names (delete them, or call them from production; "
        "a deliberate test seam goes in ALLOWED_ORPHANS with its reason): "
        + ", ".join(f"{key} ({kinds[key]})" for key in new)
    )


def test_every_allowed_orphan_is_still_an_orphan():
    orphans, _ = _orphans()
    stale = sorted(set(ALLOWED_ORPHANS) - orphans)
    assert not stale, f"drop these from ALLOWED_ORPHANS, they are used or gone: {', '.join(stale)}"
