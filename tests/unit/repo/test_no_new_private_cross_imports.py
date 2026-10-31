"""A name another module imports is public API: no leading underscore.

The underscore tells a reader a name is internal to its module. Importing
``_helper`` from a sibling makes that signal false, so the count of such
imports may only fall. Rename the name instead of raising the ceiling.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# Existing private cross-module imports. Lower it when one is renamed.
PRIVATE_IMPORT_CEILING = 146


def _is_submodule(module: str, name: str) -> bool:
    base = ROOT.joinpath(*module.split("."))
    return (base / f"{name}.py").is_file() or (base / name).is_dir()


def _private_imports() -> list[str]:
    found: list[str] = []
    for top in ("src", "app"):
        for path in sorted((ROOT / top).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if node.module.split(".")[0] not in ("src", "app"):
                    continue
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        if _is_submodule(node.module, alias.name):
                            continue
                        found.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} "
                            f"{node.module}.{alias.name}"
                        )
    return found


def test_private_cross_module_imports_do_not_grow():
    found = _private_imports()
    assert len(found) <= PRIVATE_IMPORT_CEILING, (
        f"{len(found)} private cross-module imports (ceiling "
        f"{PRIVATE_IMPORT_CEILING}); rename the name public instead:\n"
        + "\n".join(found)
    )
