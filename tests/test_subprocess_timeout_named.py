"""Static gate: a ``subprocess`` spawn's ``timeout=`` is a named constant, not
a bare number (#1793).

Why this is a *static* check and not a runtime one
--------------------------------------------------
A bare ``timeout=15`` or ``timeout=30`` runs exactly as well as a named one —
nothing at runtime can tell the two apart, which is exactly how two of them
(``src/life_os/history.py:51``, ``src/session_host.py:1279``) sat
unexplained long enough to be flagged twice by the maintainability audit
(#1793): no name to carry a reason, and no diff ever had to touch a constant
to ask "is 15 still right?". A name forces the one-clause reason the audit
asked for (measured, or an admitted guess) into the code next to the number.

Scope
-----
``src/`` and ``app/`` — the runtime trees. Pre-existing bare timeouts this
issue did not touch are grandfathered in ``BASELINE`` (ratchet style, as
``.static-ratchet-baseline.json`` does for ``scripts/static_ratchet.py``):
shrink it by naming one and deleting its entry, never grow it silently.

Deliberately literal-only: a ``timeout=`` built from an expression (a name, an
attribute, a call) already carries a name or a reason one hop away, so only a
bare ``ast.Constant`` number is judged.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src", "app")
SPAWN_ATTRS = {"run", "Popen", "call", "check_output", "check_call"}

#: Pre-existing bare timeouts, not part of #1793's two findings — fix and
#: remove an entry in its own PR rather than growing this set.
BASELINE = {
    ("src/session_hosting/remote.py", 332),  # taskkill on a detached session's kill
    ("app/webapp/manager.py", 115),     # tray subprocess readiness probe
    ("app/tray/tailscale.py", 58),
    ("app/tray/tray.py", 275),
    ("src/usage/quota.py", 559),
}


def _bare_timeout_spawns_in(tree: ast.AST, label: str) -> list[tuple[str, int]]:
    """``(label, line)`` for every ``subprocess.<spawn>(..., timeout=<number literal>, ...)``."""
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (
            isinstance(fn, ast.Attribute)
            and fn.attr in SPAWN_ATTRS
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "subprocess"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "timeout":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, (int, float)):
                offenders.append((label, node.lineno))
    return offenders


def _runtime_py_files() -> list[Path]:
    files: list[Path] = []
    for rel in SCAN_DIRS:
        for py in sorted((PROJECT_ROOT / rel).rglob("*.py")):
            if "__pycache__" not in py.parts:
                files.append(py)
    return files


def test_no_bare_numeric_timeout_on_a_subprocess_spawn():
    offenders: list[tuple[str, int]] = []
    scanned = 0
    for py in _runtime_py_files():
        label = py.relative_to(PROJECT_ROOT).as_posix()
        scanned += 1
        offenders.extend(_bare_timeout_spawns_in(ast.parse(py.read_text(encoding="utf-8")), label))

    assert scanned > 0, "the scan found no files — SCAN_DIRS is wrong"
    new = sorted(set(offenders) - BASELINE)
    assert not new, (
        "a subprocess spawn's timeout is a bare number with no name or reason (#1793) — "
        "give it a named constant and a one-clause reason (measured, or an admitted guess):\n  "
        + "\n  ".join(f"{label}:{line}" for label, line in new)
    )


def test_the_baseline_entries_are_still_real():
    """A baseline entry that no longer matches is stale — either the line
    moved (fix the line number) or it was named (delete the entry)."""
    offenders = set()
    for py in _runtime_py_files():
        label = py.relative_to(PROJECT_ROOT).as_posix()
        offenders.update(_bare_timeout_spawns_in(ast.parse(py.read_text(encoding="utf-8")), label))
    stale = sorted(BASELINE - offenders)
    assert not stale, f"BASELINE entries no longer match a bare timeout — fix or remove: {stale}"


def test_the_matcher_recognizes_the_shapes_it_must():
    """The gate's own logic, on synthetic sources — so a matcher that quietly
    stopped matching anything can't pass by finding nothing."""
    src = (
        "import subprocess\n"
        "subprocess.run(cmd, timeout=15)\n"              # 2: caught — bare int
        "subprocess.run(cmd, timeout=2.5)\n"              # 3: caught — bare float
        "subprocess.run(cmd, timeout=_TIMEOUT_S)\n"       # 4: named, fine
        "subprocess.Popen(cmd, timeout=_TIMEOUT_S())\n"   # 5: a call, fine
        "proc.wait(timeout=5)\n"                          # 6: not a subprocess.* spawn
    )
    lines = {line for _, line in _bare_timeout_spawns_in(ast.parse(src), "synthetic")}
    assert lines == {2, 3}, f"matcher flagged {sorted(lines)}"
