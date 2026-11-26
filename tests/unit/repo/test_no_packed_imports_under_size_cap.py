"""Static gate: a module stays under the file-size cap by being split, never by
packing its imports (#2668).

The structure ratchet counts physical lines, so a parenthesised import with
several names per line buys room under the cap without making the module any
smaller. This counts such a module at one name per line.
"""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.structure_ratchet import DEFAULT_MAX_FILE_LINES

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _lines_saved_by_packing(source: str) -> int:
    """Lines ``source`` would gain with one name per line in each multi-line import."""
    return sum(
        len(node.names) - len({alias.lineno for alias in node.names})
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.end_lineno != node.lineno
    )


def test_no_module_is_under_the_size_cap_only_by_packing_imports():
    offenders: list[str] = []
    scanned = 0
    for root in ("src", "app"):
        for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            scanned += 1
            lines = len(source.splitlines())
            unpacked = lines + _lines_saved_by_packing(source)
            if lines <= DEFAULT_MAX_FILE_LINES < unpacked:
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}: "
                    f"{lines} lines, {unpacked} at one imported name per line"
                )

    assert scanned > 0, "the scan found no files"
    assert not offenders, (
        "split the module instead of packing its imports:\n  " + "\n  ".join(offenders)
    )


def test_the_counter_sees_only_packed_multi_line_imports():
    assert _lines_saved_by_packing("from a import (\n    b, c, d,\n    e,\n)\n") == 2
    assert _lines_saved_by_packing("from a import (\n    b,\n    c,\n)\n") == 0
    assert _lines_saved_by_packing("from a import b, c\n") == 0
