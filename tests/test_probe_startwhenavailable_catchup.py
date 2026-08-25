"""probe-startwhenavailable-catchup.ps1's ``-Check`` must not throw on a
dd/MM host (#786).

The probe persists its fire time as an ISO 8601 (``'o'`` format) string,
which is culture-invariant on write. But PowerShell's ``ConvertFrom-Json``
auto-converts an ISO date-shaped JSON string into a ``[datetime]`` object,
and PowerShell then coerces that object back to an invariant-format string
(``"MM/dd/yyyy HH:mm:ss"``) when it is handed to ``[datetime]::Parse()`` as a
bare string argument - which parses that string under the AMBIENT culture.
On a dd/MM host (es-ES), a fire time with day-of-month > 12 makes that
coerced string invalid and ``Parse`` throws before ``-Check`` can print a
verdict.

This seeds a real state file exactly as ``-Arm`` would (day 23, matching the
reported crash's shape), then runs ``-Check`` against the real script under
``CurrentCulture=es-ES`` in a real ``pwsh`` child process - the only shape
that reproduces production, since a test running under this suite's own
locale would pass against the pre-fix code and prove nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "probe-startwhenavailable-catchup.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not on PATH")


def _run_check_under_culture(culture: str, fire_day: int) -> subprocess.CompletedProcess:
    state_path = Path(tempfile.gettempdir()) / "app-launcher-757-catchup-probe.json"
    log_path = Path(tempfile.gettempdir()) / "app-launcher-757-catchup-probe.log"
    state = {
        # Written exactly as Invoke-Arm writes it: ToString('o').
        "armed_at": "2026-01-01T00:00:00.0000000+01:00",
        "fire_at": f"2026-01-{fire_day:02d}T12:00:00.0000000+01:00",
        "logon_type": "Interactive",
        "task": r"\_zz-757-catchup-probe",
        "log": str(log_path),
    }
    log_path.unlink(missing_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    try:
        command = (
            f"[Threading.Thread]::CurrentThread.CurrentCulture = '{culture}'; "
            f"& '{SCRIPT}' -Check"
        )
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )
    finally:
        state_path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)


class TestCheckSurvivesDdMmCulture:
    def test_check_does_not_throw_under_es_es_with_day_over_12(self):
        result = _run_check_under_culture("es-ES", fire_day=23)
        assert result.returncode == 0, (
            "-Check crashed under es-ES with a day-23 fire time - #786 regressing.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "was not recognized as a valid DateTime" not in result.stderr
        assert "VERDICT" in result.stdout

    def test_check_still_works_under_en_us(self):
        """Control: the en-US host this was authored on must keep working too."""
        result = _run_check_under_culture("en-US", fire_day=23)
        assert result.returncode == 0, (
            f"-Check crashed under en-US.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "VERDICT" in result.stdout
