"""Guards against the #822 shape: the readback child clobbering repo source.

The bug was not "one path was wrong" — it was that a test process wrote to a
mis-resolved path, destroyed a tracked source file, and the run still reported
green. These tests pin both halves of the fix: the child refuses an unsafe
destination, and it takes that destination from the environment (which
pywinpty cannot tokenise) rather than from argv (which it demonstrably does).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

_CHILD = Path(__file__).parent / "_pty_readback_child.py"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_child(dest: str | None, extra_argv: list[str] | None = None):
    """Run the child directly (no PTY) and return the completed process.

    Without a real console the raw-mode ``SetConsoleMode`` call is a no-op that
    still returns, so the destination checks — which run *before* it — are
    reachable here. That is the whole point: the refusal must happen before any
    file is opened.
    """
    env = {k: v for k, v in os.environ.items() if k != "PTY_READBACK_RESULT"}
    if dest is not None:
        env["PTY_READBACK_RESULT"] = dest
    return subprocess.run(
        [sys.executable, str(_CHILD), *(extra_argv or [])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=NO_WINDOW,
    )


def test_child_refuses_to_write_a_python_file(tmp_path: Path) -> None:
    """A ``.py`` destination is refused — this is the exact byte-for-byte shape
    that truncated ``_pty_readback_child.py`` and removed 3 tests silently."""
    victim = tmp_path / "victim.py"
    victim.write_text("# 200 bytes of precious source\n" * 8, encoding="utf-8")
    before = victim.read_bytes()

    proc = _run_child(str(victim))

    assert proc.returncode != 0, "child accepted a .py destination"
    assert "refusing to write a .py destination" in (proc.stderr + proc.stdout)
    assert victim.read_bytes() == before, "child truncated the .py it was pointed at"
    assert not (tmp_path / "victim.py.ready").exists(), "child got as far as the ready marker"


def test_child_refuses_to_write_inside_the_checkout() -> None:
    """Even a non-``.py`` destination inside the repo is refused: the readback
    target is always a pytest tmp_path, so anything in-tree is a resolution bug."""
    proc = _run_child(str(_REPO_ROOT / "tests" / "should-never-appear.bin"))

    assert proc.returncode != 0
    assert "refusing to write inside the checkout" in (proc.stderr + proc.stdout)
    assert not (_REPO_ROOT / "tests" / "should-never-appear.bin").exists()


def test_child_requires_the_destination_in_the_environment() -> None:
    """No env var → loud exit, never a fallback to argv.

    The regression is specifically that a positional destination is at the
    mercy of pywinpty's ``shlex.split``; accepting argv as a fallback would
    quietly reopen it.
    """
    proc = _run_child(None)
    assert proc.returncode != 0
    assert "PTY_READBACK_RESULT is unset" in (proc.stderr + proc.stdout)


def test_child_ignores_a_positional_destination(tmp_path: Path) -> None:
    """A stray positional argument must not become the destination."""
    positional = tmp_path / "from-argv.bin"
    proc = _run_child(None, extra_argv=[str(positional)])

    assert proc.returncode != 0, "child honoured a positional destination"
    assert "PTY_READBACK_RESULT is unset" in (proc.stderr + proc.stdout)
    assert not positional.exists()
    assert not Path(str(positional) + ".ready").exists()


@pytest.mark.parametrize(
    "dest_dir",
    ["plain", "with space"],
    ids=["space-free", "spaced"],
)
def test_env_destination_survives_pywinpty_tokenising(tmp_path: Path, dest_dir: str) -> None:
    """The destination reaches the child byte-exact, spaces included.

    Pre-fix this was passed positionally through a string command that pywinpty
    runs through ``shlex.split(cmd, posix=False)``; a spaced path split into two
    tokens and the child wrote to the truncated prefix. Asserting the *spaced*
    case is what proves the env-var route is immune rather than merely untested.
    """
    winpty = pytest.importorskip("winpty", reason="pywinpty (Windows ConPTY) is required")

    target_dir = tmp_path / dest_dir
    target_dir.mkdir()
    result = target_dir / "readback.bin"
    ready = Path(str(result) + ".ready")

    cmd = f"{sys.executable} {_CHILD}"
    env = {**os.environ, "PTY_READBACK_RESULT": str(result)}
    pty = winpty.PtyProcess.spawn(cmd, cwd=str(_CHILD.parent), dimensions=(40, 120), env=env)
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), f"child never signalled readiness for {result}"

        pty.write("PAYLOAD<<<EOP>>>")
        for _ in range(200):
            if result.exists():
                break
            time.sleep(0.05)
    finally:
        try:
            pty.close(force=True)
        except Exception:
            pass

    assert result.exists(), f"child did not write to the intended path {result}"
    assert result.read_bytes() == b"PAYLOAD"
    # Nothing landed at a truncated prefix of the path.
    assert not (tmp_path / "with").exists()
