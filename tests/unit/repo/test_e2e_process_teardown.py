"""The e2e gate's teardown leaves no stub child behind (issue #1335).

Two gate runs left ``e2e_stub_child.py`` (and the ``OpenConsole.exe`` hosting
it) alive after the disposable session-host had gone, pinning the worktree.
These pin both layers of ``tests/e2e/_process_teardown.py`` with real
processes: the tree-aware stop of a server, and the argv-scoped reap of any
stub that escaped it.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import List

import psutil
import pytest

from tests.e2e._process_teardown import (
    _is_stub_under,
    reap_stub_children,
    stub_children,
    terminate_tree,
)
from tests.e2e.stub_session import STUB_SCRIPT_NAME, write_claude_shim

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")

_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# A server stand-in: spawns one long-lived child the way the session-host
# spawns a PTY, reports its pid, then idles until it is stopped.
_PARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _kill_quietly(procs: List[psutil.Process]) -> None:
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


def _start_stub(shim_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-X", "utf8", str(shim_dir / STUB_SCRIPT_NAME)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, creationflags=_FLAGS,
    )


def _wait_for_stubs(scope: Path, count: int) -> None:
    deadline = time.time() + 10
    while len(stub_children(scope)) < count and time.time() < deadline:
        time.sleep(0.1)


def test_terminate_tree_reaps_a_child_that_outlives_its_parent() -> None:
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT], stdout=subprocess.PIPE, text=True,
        creationflags=_FLAGS,
    )
    child = psutil.Process(int(parent.stdout.readline()))
    tree = [child, *child.children(recursive=True)]
    try:
        terminate_tree(parent)
        _, alive = psutil.wait_procs(tree, timeout=5)
        assert not alive, f"outlived the stopped parent: {[p.pid for p in alive]}"
    finally:
        _kill_quietly(tree)
        parent.kill()


def test_reap_stub_children_kills_only_its_own_scope(tmp_path: Path) -> None:
    mine, other = tmp_path / "mine", tmp_path / "other"
    for shim_dir in (mine, other):
        shim_dir.mkdir()
        write_claude_shim(shim_dir)
    started = [_start_stub(mine), _start_stub(other)]
    try:
        _wait_for_stubs(mine, 1)
        _wait_for_stubs(other, 1)
        assert reap_stub_children(mine), "a live stub under the scope was not found"
        assert stub_children(mine) == []
        assert stub_children(other), "a stub outside the scope was reaped"
    finally:
        _kill_quietly(stub_children(tmp_path))
        for proc in started:
            proc.kill()


def test_stub_match_is_a_whole_argv_element() -> None:
    checkout = Path("E:/work/repo-wt-7")
    stub = f"C:/Temp/pytest-of-u/pytest-3/popen-gw1/claude-shim0/{STUB_SCRIPT_NAME}"
    interpreter = str(checkout / ".venv/Scripts/python.exe")
    assert _is_stub_under([interpreter, "-X", "utf8", stub], checkout)
    assert _is_stub_under([interpreter, stub], Path(stub).parent)
    # A shell that only mentions both paths is not a stub child.
    assert not _is_stub_under(["bash", "-c", f"grep x {stub} {interpreter}"], checkout)
    assert not _is_stub_under([interpreter, "-X", "utf8", stub], Path("E:/work/repo"))
