"""verify-before-ship.ps1's e2e mutex wait (issue #1682).

A gate that exits without ReleaseMutex() leaves `Global\\AppLauncherFullE2EGate`
abandoned, and .NET hands that to the *next* waiter as an
AbandonedMutexException from WaitOne() -- which means the waiter now owns it.
The gate used to let that exception reach its unhandled-error path and fail.
`Wait-E2EGateMutex` (scripts/e2e-gate-route.ps1) counts it as acquired.

Driven through real Windows PowerShell 5.1 against a test-only mutex name, so a
real gate run on this machine is never disturbed. One PowerShell process per
scenario; the "abandoner" is a separate process that exits holding the mutex.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import uuid

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
ROUTE_PS1 = REPO_ROOT / "scripts" / "e2e-gate-route.ps1"
POWERSHELL = (pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the gate is a Windows PowerShell script")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Takes the mutex and exits without releasing it: the dead gate holder.
_ABANDONER = r"""
$m = New-Object System.Threading.Mutex($false, $env:E2E_TEST_MUTEX)
if (-not $m.WaitOne(0)) { exit 3 }
exit 0
"""

# A named mutex is destroyed when its last handle closes, so it is only
# "abandoned" if another process still holds a handle when the owner dies --
# the queued gate in the incident did. The waiter therefore creates its handle
# first, then runs the abandoner as a child (when $env:E2E_TEST_ABANDON is set).
_OPEN_AND_ABANDON = r"""
$m = New-Object System.Threading.Mutex($false, $env:E2E_TEST_MUTEX)
if ($env:E2E_TEST_ABANDON) {
    $child = Start-Process -FilePath (Get-Process -Id $PID).Path -Wait -PassThru -WindowStyle Hidden `
        -ArgumentList @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $env:E2E_TEST_ABANDON)
    if ($child.ExitCode -ne 0) { throw "abandoner exited $($child.ExitCode)" }
}
"""

# Waits the way the gate did before the fix: a bare WaitOne, no catch.
_NAIVE_WAIT = _OPEN_AND_ABANDON + r"""
try { [void]$m.WaitOne(0); 'acquired' } catch { $_.Exception.InnerException.GetType().Name }
"""

# The gate's wait, then the same release the gate's `finally` does.
_HELPER_WAIT = r"""
. $env:E2E_ROUTE_PS1
""" + _OPEN_AND_ABANDON + r"""
$r = Wait-E2EGateMutex -Mutex $m -Timeout ([TimeSpan]::FromMilliseconds([int]$env:E2E_TEST_WAIT_MS))
if ($r.Acquired) { $m.ReleaseMutex() }
$m.Dispose()
$r | ConvertTo-Json -Compress
"""

# Holds the mutex until told to stop, so a live holder is observable.
_LIVE_HOLDER = r"""
$m = New-Object System.Threading.Mutex($false, $env:E2E_TEST_MUTEX)
[void]$m.WaitOne(0)
'held'
[void][Console]::In.ReadLine()
$m.ReleaseMutex()
"""


def _ps(script: str, mutex: str, **extra: str) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, E2E_TEST_MUTEX=mutex, E2E_ROUTE_PS1=str(ROUTE_PS1), **extra),
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=60,
    )


@pytest.fixture
def mutex_name() -> str:
    return f"Global\\AppLauncherE2EGateTest-{uuid.uuid4().hex}"


def _abandoner() -> str:
    return base64.b64encode(_ABANDONER.encode("utf-16-le")).decode("ascii")


def test_a_bare_wait_on_an_abandoned_mutex_throws(mutex_name: str) -> None:
    """The control: this is the exception that aborted the gate at line 186."""
    out = _ps(_NAIVE_WAIT, mutex_name, E2E_TEST_ABANDON=_abandoner())
    assert out.stdout.strip() == "AbandonedMutexException", out.stderr


def test_an_abandoned_mutex_counts_as_acquired(mutex_name: str) -> None:
    got = json.loads(_ps(_HELPER_WAIT, mutex_name, E2E_TEST_WAIT_MS="0", E2E_TEST_ABANDON=_abandoner()).stdout)
    assert got == {"Acquired": True, "Abandoned": True}


def test_a_free_mutex_is_acquired_without_the_abandoned_flag(mutex_name: str) -> None:
    got = json.loads(_ps(_HELPER_WAIT, mutex_name, E2E_TEST_WAIT_MS="0").stdout)
    assert got == {"Acquired": True, "Abandoned": False}


def test_a_live_holder_still_times_out(mutex_name: str) -> None:
    holder = subprocess.Popen(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", base64.b64encode(_LIVE_HOLDER.encode("utf-16-le")).decode("ascii")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, E2E_TEST_MUTEX=mutex_name), creationflags=_NO_WINDOW,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        got = json.loads(_ps(_HELPER_WAIT, mutex_name, E2E_TEST_WAIT_MS="300").stdout)
        assert got == {"Acquired": False, "Abandoned": False}
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.close()
        holder.wait(timeout=30)


def test_the_gate_waits_through_the_helper_never_a_bare_waitone() -> None:
    """Both waits (the immediate try and the queued one) must go through the helper."""
    gate = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    assert "WaitOne(" not in gate
    assert gate.count("Wait-E2EGateMutex -Mutex") == 2
