"""Every e2e module must carry the ``smoke`` marker at module level (#1007).

The marker is what puts a module into the two commands that actually run the
suite, and a module without it falls between them rather than failing loudly:

* ``scripts/run-e2e.ps1`` selects with ``-m smoke``, so an unmarked module is
  silently **deselected** -- its regressions stop being checked at all;
* the documented non-browser command ``pytest tests -m "not smoke"``
  (``CLAUDE.md``, ``README.md``) instead **collects** it, and the live-tray
  guard in ``tests/e2e/conftest.py`` aborts the whole run with
  ``pytest.exit(returncode=2)``.

Nothing re-marks these modules at collection time (the one
``pytest_collection_modifyitems``, in ``tests/e2e/conftest.py``, only
deselects WebKit nodes, #1220), so each module's own ``pytestmark`` is the
only source of truth.

The check parses the module with :mod:`ast` rather than importing it (no
Playwright needed) and rather than matching a fixed line, because both
declaration styles in the tree are legitimate::

    pytestmark = pytest.mark.smoke
    pytestmark = [pytest.mark.smoke, pytest.mark.usefixtures("...")]

It deliberately lives outside ``tests/e2e/`` so it runs in the non-browser
suite -- a guard inside the e2e tree would need the very marker it guards.
"""

from __future__ import annotations

import ast
from pathlib import Path

_E2E_DIR = Path(__file__).resolve().parent / "e2e"


def _declares_smoke(source: str) -> bool:
    """True when the module assigns a ``pytestmark`` that includes ``smoke``."""
    tree = ast.parse(source)
    for node in tree.body:  # module level only -- a nested one wouldn't apply
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            continue
        marks = node.value.elts if isinstance(node.value, ast.List) else [node.value]
        for mark in marks:
            # `pytest.mark.smoke` is an Attribute chain; anything else
            # (e.g. `pytest.mark.usefixtures(...)`, a Call) is not it.
            if (
                isinstance(mark, ast.Attribute)
                and mark.attr == "smoke"
                and isinstance(mark.value, ast.Attribute)
                and mark.value.attr == "mark"
            ):
                return True
    return False


def test_every_e2e_module_declares_the_smoke_marker():
    modules = sorted(_E2E_DIR.glob("test_*.py"))
    assert modules, "no e2e test modules found -- wrong path?"
    missing = [
        p.name for p in modules if not _declares_smoke(p.read_text(encoding="utf-8"))
    ]
    assert not missing, (
        "e2e module(s) without a module-level `smoke` marker: "
        + ", ".join(missing)
        + " -- an unmarked module is deselected by scripts/run-e2e.ps1 (-m smoke) "
        "and aborts `pytest tests -m \"not smoke\"` via the live-tray guard (#1007)."
    )
