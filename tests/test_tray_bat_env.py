"""tray.bat must not export a generic ``APP_NAME`` to the tray it starts (#963).

cmd's ``setlocal`` only scopes a variable away from the *calling* console;
every process tray.bat launches still inherits it. The tray -> webapp ->
session-host chain then handed ``APP_NAME=AppLauncher`` to every Coding
session and launched app, where other projects legitimately read
``APP_NAME`` as their own config key (website-analytics'
``os.getenv("APP_NAME", "Website Analytics")``).

The test runs a throwaway copy of the real tray.bat with ``USERPROFILE``
pointed at a temp home whose ``tray_lifecycle.ps1`` is a stub. The stub
records the environment it inherited plus the arguments it was given, so
nothing real is detected, killed or started.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parent.parent

# ASCII only: Windows PowerShell 5.1 runs this and mis-parses non-ASCII.
_STUB_PS1 = """\
$state = if (Test-Path Env:APP_NAME) { 'present=' + $env:APP_NAME } else { 'absent' }
$lines = @(('APP_NAME_ENV=' + $state), ('ARGS=' + ($args -join '|')))
Set-Content -Path $env:TRAY_STUB_OUT -Value $lines -Encoding ASCII
exit 0
"""


@pytest.mark.skipif(sys.platform != "win32", reason="tray.bat is Windows-only")
def test_tray_bat_does_not_leak_app_name_to_the_tray(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bat = repo / "tray.bat"
    shutil.copyfile(REPO_ROOT / "tray.bat", bat)

    home = tmp_path / "home"
    stub = home / ".claude" / "tray" / "tray_lifecycle.ps1"
    stub.parent.mkdir(parents=True)
    stub.write_text(_STUB_PS1, encoding="ascii")
    out = tmp_path / "stub_out.txt"

    # This test may itself run under a launcher-hosted session that still
    # carries the leaked variable, so start from an environment without it.
    env = {k: v for k, v in os.environ.items() if k.upper() != "APP_NAME"}
    env["USERPROFILE"] = str(home)
    env["TRAY_STUB_OUT"] = str(out)

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(bat)],
        env=env,
        capture_output=True,
        timeout=60,
        creationflags=NO_WINDOW,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lines = dict(
        line.split("=", 1)
        for line in out.read_text(encoding="ascii").splitlines()
        if "=" in line
    )
    assert lines["APP_NAME_ENV"] == "absent"
    # The tray's display name still reaches the lifecycle helper as its argument.
    assert "|-AppName|AppLauncher|" in lines["ARGS"]
