"""Drift guard: every runtime ``subprocess`` spawn suppresses the console (#688).

The fleet-wide convention ("Subprocess spawns must suppress the console window
(Windows)") was consolidated into ``src/subprocess_flags.py`` by #585 — but the
sweep stopped at ``src/`` + ``app/`` and never reached ``scripts/``, so a chain
of three console windows kept flashing on every tray-driven webapp restart on a
Tailscale machine. Nothing caught it, because an unsuppressed spawn only
misbehaves under a *console-less* parent (the ``pythonw`` tray and its
descendants) — never in the terminal where the tests run.

So the gate is static: parse the runtime trees and assert every
``subprocess.<spawn>(...)`` passes ``creationflags`` resolving to the shared
``NO_WINDOW`` / ``NO_WINDOW_NEW_GROUP`` constant. ``tests/`` is deliberately
out of scope — it runs from a real console and several cases assert on spawn
kwargs. Sibling implementation: ``fleet-config``'s
``tests/acceptance/spawn_scanner.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Trees whose Python is runtime code — reachable from the console-less tray.
_SCAN_DIRS = ("src", "app", "scripts")
_SPAWN_ATTRS = {"run", "Popen", "call", "check_output", "check_call"}

#: The convention's one carve-out — "only omit the flag when the window is
#: meant to be visible to the user (rare — e.g. a deliberately-opened
#: interactive terminal)". Keyed by ``path::function`` rather than line so it
#: survives edits above it; adding an entry is a deliberate, reviewable act.
_VISIBLE_CONSOLE_EXEMPT = {
    "src/launcher.py::spawn_bat": (
        "Chooses its own flag per launch (#790): NO_WINDOW for the Apps "
        "tab's stealth button, CREATE_NEW_CONSOLE on purpose for its ⚡ "
        "one — a visible CMD window the user watches and closes."
    ),
}


def _resolves_to_no_window(node: ast.AST) -> bool:
    """True when a ``creationflags=`` value provably resolves to the shared
    constant — directly, via attribute access, or OR'd into a combined
    expression. Deliberately conservative: a bare ``0`` or a re-inlined
    ``subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0`` ternary
    (the exact drift the shared module exists to retire) does *not* resolve.
    """
    if isinstance(node, ast.Name):
        return node.id in {"NO_WINDOW", "NO_WINDOW_NEW_GROUP"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"NO_WINDOW", "NO_WINDOW_NEW_GROUP"}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _resolves_to_no_window(node.left) or _resolves_to_no_window(node.right)
    return False


def _dict_provides_flags(node: ast.AST) -> bool:
    """True when a ``dict(...)`` call or ``{...}`` literal carries a
    NO_WINDOW-resolving ``creationflags`` entry."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return any(
            kw.arg == "creationflags" and _resolves_to_no_window(kw.value)
            for kw in node.keywords
        )
    if isinstance(node, ast.Dict):
        return any(
            isinstance(key, ast.Constant)
            and key.value == "creationflags"
            and _resolves_to_no_window(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _walk_scope(scope: ast.AST):
    """Every node belonging to ``scope`` itself, never crossing into a nested
    function body. Keeps a kwargs local from being resolved against a
    same-named one in an enclosing scope, in either direction."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, _SCOPE_NODES):
            continue
        yield child
        yield from _walk_scope(child)


def _kwarg_locals(scope: ast.AST) -> Dict[str, bool]:
    """Map ``name -> provides creationflags`` for kwargs dicts built in
    ``scope``, covering both ``kw = dict(...)`` and ``kw["creationflags"] = ...``.
    Several call sites here build their kwargs into a local and splat it
    (``subprocess.Popen(cmd, **kw)``), so the scan has to look through that
    indirection rather than report a false positive."""
    provides: Dict[str, bool] = {}
    for node in _walk_scope(scope):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                provides[target.id] = _dict_provides_flags(value)
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "creationflags"
                and _resolves_to_no_window(value)
            ):
                provides[target.value.id] = True
    return provides


def _offenders_in_tree(tree: ast.Module, label: str) -> List[str]:
    """``label:line`` for every ``subprocess.<spawn>(...)`` in ``tree`` that
    omits ``creationflags`` or passes a value that doesn't provably resolve."""
    offenders: List[str] = []
    scopes: List[tuple[ast.AST, str]] = [(tree, "<module>")]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node, node.name))

    for scope, scope_name in scopes:
        if f"{label}::{scope_name}" in _VISIBLE_CONSOLE_EXEMPT:
            continue
        provides = _kwarg_locals(scope)
        for node in _walk_scope(scope):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (
                isinstance(fn, ast.Attribute)
                and fn.attr in _SPAWN_ATTRS
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "subprocess"
            ):
                continue
            direct: Optional[ast.keyword] = next(
                (kw for kw in node.keywords if kw.arg == "creationflags"), None
            )
            if direct is not None and _resolves_to_no_window(direct.value):
                continue
            splatted = any(
                kw.arg is None
                and isinstance(kw.value, ast.Name)
                and provides.get(kw.value.id, False)
                for kw in node.keywords
            )
            if splatted:
                continue
            offenders.append(f"{label}:{node.lineno}")
    return sorted(set(offenders))


def _runtime_python_files() -> List[Path]:
    files: List[Path] = []
    for rel in _SCAN_DIRS:
        for py in sorted((_REPO_ROOT / rel).rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            files.append(py)
    return files


def test_every_runtime_subprocess_spawn_suppresses_the_console():
    offenders: List[str] = []
    for py in _runtime_python_files():
        label = py.relative_to(_REPO_ROOT).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        offenders.extend(_offenders_in_tree(tree, label))
    assert not offenders, (
        "subprocess spawn(s) missing a creationflags= that resolves to "
        "src.subprocess_flags.NO_WINDOW / NO_WINDOW_NEW_GROUP:\n  "
        + "\n  ".join(offenders)
    )


def test_no_runtime_file_imports_a_spawn_name_directly():
    """``from subprocess import Popen`` is bare-name-called, so the scan above
    cannot see it. Keeping the count at zero is what makes that scan a sound
    gate rather than a partial one."""
    offenders: List[str] = []
    for py in _runtime_python_files():
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "subprocess"
                and any(alias.name in _SPAWN_ATTRS for alias in node.names)
            ):
                offenders.append(f"{py.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}")
    assert not offenders, (
        "`from subprocess import <spawn>` evades the creationflags scan:\n  "
        + "\n  ".join(offenders)
    )


def test_visible_console_exemptions_still_point_at_real_functions():
    """A stale exemption silently widens the gate — fail if one no longer
    resolves to a function that actually spawns something."""
    stale: List[str] = []
    for key in _VISIBLE_CONSOLE_EXEMPT:
        rel, _, func = key.partition("::")
        path = _REPO_ROOT / rel
        if not path.is_file():
            stale.append(f"{key} (no such file)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if func not in names:
            stale.append(f"{key} (no such function)")
    assert not stale, "stale _VISIBLE_CONSOLE_EXEMPT entries:\n  " + "\n  ".join(stale)


def test_scanner_rejects_the_shapes_the_convention_forbids():
    """Negative cases — a value that merely *has* the keyword must not pass."""
    src = (
        "import subprocess\n"
        "import sys\n"
        "subprocess.run(cmd, creationflags=0)\n"                                       # 3
        "subprocess.run(cmd, creationflags=(subprocess.CREATE_NO_WINDOW\n"
        "                                   if sys.platform == 'win32' else 0))\n"     # 4
        "subprocess.run(cmd)\n"                                                        # 6
        "subprocess.run(cmd, creationflags=NO_WINDOW)\n"                               # 7
        "subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP\n"
        "                 | NO_WINDOW)\n"                                              # 8
        "kw = dict(cwd='.', creationflags=NO_WINDOW_NEW_GROUP)\n"                      # 10
        "subprocess.Popen(cmd, **kw)\n"                                                # 11
        "bad = dict(cwd='.')\n"                                                        # 12
        "subprocess.Popen(cmd, **bad)\n"                                               # 13
    )
    flagged = {int(o.rsplit(":", 1)[1]) for o in _offenders_in_tree(ast.parse(src), "synthetic")}
    assert 3 in flagged, "creationflags=0 must be reported"
    assert 4 in flagged, "a re-inlined win32 ternary must be reported"
    assert 6 in flagged, "a spawn with no creationflags must be reported"
    assert 13 in flagged, "a splatted kwargs dict without the flag must be reported"
    assert 7 not in flagged, "creationflags=NO_WINDOW must pass"
    assert 8 not in flagged, "CREATE_NEW_PROCESS_GROUP | NO_WINDOW must pass"
    assert 11 not in flagged, "a splatted kwargs dict carrying the flag must pass"


def test_a_kwargs_local_never_resolves_across_a_function_boundary():
    """An enclosing scope's same-named kwargs dict must not vouch for a spawn
    inside a nested function — that would silently widen the gate."""
    src = (
        "import subprocess\n"
        "def outer():\n"
        "    kw = dict(creationflags=NO_WINDOW)\n"
        "    def inner():\n"
        "        kw = dict(cwd='.')\n"
        "        subprocess.Popen(cmd, **kw)\n"   # 6 — inner's kw has no flag
        "    subprocess.Popen(cmd, **kw)\n"       # 7 — outer's kw does
    )
    flagged = {int(o.rsplit(":", 1)[1]) for o in _offenders_in_tree(ast.parse(src), "synthetic")}
    assert 6 in flagged, "the nested spawn must not inherit the outer scope's flagged dict"
    assert 7 not in flagged, "the outer spawn's own dict still vouches for it"
