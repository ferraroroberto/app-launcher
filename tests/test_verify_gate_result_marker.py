"""The pre-ship gate's machine-greppable verdict line (issue #1086).

A pipeline's status is its *last* command's status, so
`pwsh -File scripts/verify-before-ship.ps1 | tail -40` hands the caller
`tail`'s 0 and the gate's own 1 disappears -- a verification gate reporting
green on a red run, silently, with a human reading scrollback the only thing
that catches it. The fix that a pipe cannot erase is in the payload: a final
`GATE-RESULT: PASS` / `GATE-RESULT: FAIL` line on *both* exit paths, so its
absence is a signal too.

The FAIL path is driven for real here -- a copy of the gate in a temp tree with
no `.venv` takes the script's own `Fail` within a second, through a real
anonymous pipe (`stdout=PIPE` is the same kernel object `|` creates). The PASS
path costs ~19 minutes to reach, so it is pinned statically, along with the
rule that no exit path may skip the marker.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE_PS1 = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = (pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="the gate is a Windows PowerShell script")


def _run_isolated_gate(tmp_path: pathlib.Path) -> subprocess.CompletedProcess:
    """Run a copy of the real gate in a throwaway tree, capturing stdout.

    The gate resolves its repo root from `$PSScriptRoot`, so a copy under
    `tmp_path/scripts/` sees `tmp_path` as the repo -- a tree with no `.venv`,
    which trips its very first check within a second. `capture_output` is an
    anonymous pipe, the same kernel object `|` creates, so what these tests
    read is exactly what `... | tail -40` would hand a caller.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    shutil.copy2(GATE_PS1, scripts / GATE_PS1.name)
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(scripts / GATE_PS1.name)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=120,
    )


def _stdout_lines(run: subprocess.CompletedProcess) -> list[str]:
    lines = [line.rstrip() for line in run.stdout.splitlines() if line.strip()]
    assert lines, f"nothing on stdout; stderr={run.stderr!r}"
    return lines


@windows_only
def test_fail_path_exits_nonzero_and_names_itself_through_a_pipe(tmp_path: pathlib.Path) -> None:
    """The real `Fail` path, over a pipe: exit 1 *and* the marker."""
    run = _run_isolated_gate(tmp_path)

    assert run.returncode == 1, f"stdout={run.stdout!r} stderr={run.stderr!r}"
    lines = _stdout_lines(run)
    assert lines[-1] == "GATE-RESULT: FAIL", lines[-5:]
    # The human-readable reason is still there -- the marker adds to the
    # output, it does not replace it.
    assert any(".venv missing" in line for line in lines), lines


@windows_only
def test_terminating_error_reports_itself_on_stdout_instead_of_vanishing(
        tmp_path: pathlib.Path) -> None:
    """The silent path: a terminating error, not a `Fail` (issue #1086).

    Before the script-scope trap this was the worst case, not an edge case.
    `$ErrorActionPreference = "Stop"` turns any unhandled error into an abort
    that printed to *stderr* and exited 1 -- so a caller piping stdout got
    **zero lines and an exit status of 0**: a completely silent, empty,
    apparently-successful gate run. Measured on pre-fix code before this test
    was written.

    Provoked naturally rather than by injecting a `throw`: a *file* where the
    gate needs to create its `webapp/` progress-log directory, so the real
    `[System.IO.Directory]::CreateDirectory` call at the top of the script
    throws for a real reason.
    """
    (tmp_path / "webapp").write_text("not a directory", encoding="utf-8")

    run = _run_isolated_gate(tmp_path)

    assert run.returncode == 1, f"stdout={run.stdout!r} stderr={run.stderr!r}"
    lines = _stdout_lines(run)
    assert lines[-1] == "GATE-RESULT: FAIL", lines[-5:]
    # ...and it must say what went wrong on stdout, not only on stderr.
    assert any("aborted on an unhandled error" in line for line in lines), lines
    assert any("already exists" in line for line in lines), lines


def _gate_text() -> str:
    return GATE_PS1.read_text(encoding="utf-8")


def test_success_path_emits_the_pass_marker_as_its_last_line() -> None:
    """The PASS half of "emitted on both paths, so absence is a signal"."""
    text = _gate_text()
    assert "Write-GateResult \"PASS\"" in text
    tail = text[text.index("[OK] Ready to ship"):]
    pass_at = tail.index("Write-GateResult \"PASS\"")
    exit_at = tail.index("\nexit 0")
    assert pass_at < exit_at, "the PASS marker must be emitted before the success exit"
    assert "Write-Host" not in tail[pass_at + len("Write-GateResult \"PASS\""):exit_at], \
        "nothing may print after the marker -- a piped caller reads the last line"


def test_no_exit_path_skips_the_marker() -> None:
    """A future third exit must carry a verdict too, or a piped caller reading
    only the last line would see the *previous* run's shape and infer nothing."""
    lines = _gate_text().splitlines()
    exits = [i for i, line in enumerate(lines) if re.match(r"\s*exit \d+\s*$", line)]
    assert exits, "the gate must exit explicitly"
    for i in exits:
        preceding = [line for line in lines[max(0, i - 3):i] if line.strip()]
        assert any("Write-GateResult" in line for line in preceding), (
            f"line {i + 1} ({lines[i].strip()!r}) exits without emitting GATE-RESULT")


def test_gate_is_ascii_for_windows_powershell_5_1() -> None:
    """Same contract as `scripts/e2e-gate-route.ps1`: the gate is run by
    Windows PowerShell 5.1, which reads a BOM-less file as ANSI."""
    GATE_PS1.read_bytes().decode("ascii")
