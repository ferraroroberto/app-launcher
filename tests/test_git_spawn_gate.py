"""Static gate: no hand-rolled ``git`` spawn outside ``src/git_utils.py`` (#1111).

Why this is a *static* check and not a runtime one
--------------------------------------------------
:func:`src.git_utils.git_env` is where ``GIT_OPTIONAL_LOCKS=0`` lives — the
fix for the stranded 0-byte ``.git/index.lock`` that froze 23 repos at one
timestamp on 2026-09-20 and nine repos for fifteen days before that
(ferraroroberto/fleet-config#939, #667). A raw
``subprocess.run(["git", ...])`` somewhere in ``src/`` or ``app/`` opts back
out of that env **invisibly**: the command still exits 0, still prints the
right answer, and the only symptom is a lock file appearing in someone else's
repo minutes later. Nothing at runtime can observe the difference, so nothing
at runtime can guard it.

This mirrors ``fleet-config``'s own ``git_wrapper`` acceptance check over
``hooks/`` / ``skills/`` / ``.claude/skills/``. That check was sound and still
missed this repo entirely — its blind spot was its *tree scope*, never its
logic, which is precisely fleet-config#939's third acceptance criterion: the
gate has to cover the tree the creator lives in.

Scope
-----
``src/`` and ``app/`` — the runtime trees the webapp and tray actually import.
Excluded on purpose:

* ``tests/`` — several tests build throwaway repos with real ``git`` calls and
  assert on spawn kwargs; forcing the wrapper there would be noise.
* ``scripts/classify_e2e.py`` — vendored **byte-verbatim** from
  ``project-scaffolding`` and deliberately self-contained (it cannot import
  this repo's ``src/``, because it also runs in consumers' trees). Its own
  missing env is real but far narrower — single-repo, gate-time only — and it
  has to be fixed upstream and re-vendored, not patched here.

Deliberately literal-only: a spawn built from a variable argv can't be judged
statically, and guessing would make the gate noisy rather than sound. Every
hand-rolled site this exists to catch is written out inline.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src", "app")
#: The one file that is *supposed* to spawn ``git``.
WRAPPER_FILES = {"src/git_utils.py"}
SPAWN_ATTRS = {"run", "Popen", "call", "check_output", "check_call"}


def _argv_head_exe(node: ast.Call) -> str | None:
    """The executable name a literal-list argv starts with, else ``None``."""
    if not node.args:
        return None
    argv = node.args[0]
    if not (isinstance(argv, ast.List) and argv.elts):
        return None
    head = argv.elts[0]
    if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
        return None
    return head.value.replace("\\", "/").rsplit("/", 1)[-1]


def _git_spawns_in(tree: ast.AST, label: str) -> list[str]:
    """``["<label>:<line>", ...]`` for every literal ``subprocess.<spawn>(["git", ...])``."""
    offenders: list[str] = []
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
        if _argv_head_exe(node) in {"git", "git.exe"}:
            offenders.append(f"{label}:{node.lineno}")
    return offenders


def _runtime_py_files() -> list[Path]:
    files: list[Path] = []
    for rel in SCAN_DIRS:
        for py in sorted((PROJECT_ROOT / rel).rglob("*.py")):
            if "__pycache__" not in py.parts:
                files.append(py)
    return files


def test_no_hand_rolled_git_spawn_outside_the_wrapper():
    offenders: list[str] = []
    scanned = 0
    for py in _runtime_py_files():
        label = py.relative_to(PROJECT_ROOT).as_posix()
        if label in WRAPPER_FILES:
            continue
        scanned += 1
        offenders.extend(_git_spawns_in(ast.parse(py.read_text(encoding="utf-8")), label))

    assert scanned > 0, "the scan found no files — SCAN_DIRS is wrong"
    assert not offenders, (
        "hand-rolled `git` spawns bypass src/git_utils.run_git, and so silently opt out of "
        "GIT_OPTIONAL_LOCKS=0 (#1111). Route them through run_git:\n  "
        + "\n  ".join(offenders)
    )


def test_the_exempted_wrapper_still_passes_an_env_to_its_spawn():
    """The exemption has to be earned.

    ``run_git`` builds ``cmd = ["git", "-C", ...]`` into a **variable** before
    spawning it, so the literal matcher above would never flag the wrapper
    anyway — the ``WRAPPER_FILES`` entry is belt-and-braces for a future
    rewrite, not load-bearing today. What *is* worth pinning statically is the
    thing a refactor silently drops: every ``subprocess`` spawn in the wrapper
    passing an ``env=``. Without it the process inherits the webapp's
    environment, ``GIT_OPTIONAL_LOCKS`` is unset, and the optional index lock
    comes back (#1111) — the behavioural proof of that lives in
    ``tests/test_git_utils.py``; this is the cheap structural half.
    """
    wrapper = PROJECT_ROOT / "src" / "git_utils.py"
    assert wrapper.is_file(), "WRAPPER_FILES points at a file that no longer exists"

    spawns = [
        node
        for node in ast.walk(ast.parse(wrapper.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SPAWN_ATTRS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert spawns, "src/git_utils.py no longer spawns anything — the exemption is stale"
    for call in spawns:
        kwargs = {kw.arg for kw in call.keywords}
        assert "env" in kwargs, (
            f"src/git_utils.py:{call.lineno} spawns without env= — GIT_OPTIONAL_LOCKS=0 "
            "would not reach git (#1111)"
        )


def test_the_matcher_recognizes_the_shapes_it_must():
    """The gate's own logic, on synthetic sources — so a matcher that quietly
    stopped matching anything can't pass by finding nothing."""
    src = (
        "import subprocess\n"
        "subprocess.run(['git', 'status'])\n"          # 2: caught
        "subprocess.run([GIT_EXE, 'status'])\n"        # 3: not literal, unjudgeable
        "subprocess.run(['gh', 'issue', 'view'])\n"    # 4: not git
        "git_utils.run_git(repo, ['status'])\n"        # 5: the wrapper
        "subprocess.Popen(['git', 'fetch'])\n"         # 6: caught
    )
    lines = {int(o.rsplit(":", 1)[1]) for o in _git_spawns_in(ast.parse(src), "synthetic")}
    assert lines == {2, 6}, f"matcher flagged {sorted(lines)}"
