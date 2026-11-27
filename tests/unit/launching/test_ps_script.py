"""The repo PowerShell launcher under the AllSigned execution policy.

`powershell -File x.ps1` refuses an unsigned script under AllSigned, and
`-ExecutionPolicy Bypass` would weaken the machine's own control. The launcher
starts the script as a ScriptBlock instead. The AllSigned runs use the
`PSExecutionPolicyPreference` environment variable: it sets the *process*-scope
policy of that one PowerShell process tree and touches no machine or user setting.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.launching import ps_script
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[3]
POWERSHELL = ps_script.POWERSHELL_EXE

_PROBE = """\
param([Parameter(Position = 0)][string] $Action, [string] $Name, [string] $Empty, [switch] $Flag)
Write-Host "action=$Action name=[$Name] empty=[$Empty] flag=$($Flag.IsPresent) root=[$PSScriptRoot]"
exit 7
"""


def _launch(script: Path, args: list[str], policy: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PSExecutionPolicyPreference": policy, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "src.launching.ps_script", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=REPO_ROOT, env=env, creationflags=NO_WINDOW,
    )


# Runs inside a console of its own with nothing redirected (an operator's terminal), then saves
# what that console actually shows, which is the only place console output can be seen.
_CONSOLE_HARNESS = """import ctypes, subprocess, sys
from ctypes import wintypes
subprocess.run([sys.executable, "-m", "src.launching.ps_script", sys.argv[1]], timeout=120)
k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = wintypes.HANDLE
console = wintypes.HANDLE(k32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None))
screen = ctypes.create_unicode_buffer(8000)
read = wintypes.DWORD()
k32.ReadConsoleOutputCharacterW(console, screen, len(screen), wintypes._COORD(0, 0), ctypes.byref(read))
open(sys.argv[2], "w", encoding="utf-8").write(screen[:read.value])
"""


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    path = tmp_path / "dir with space" / "probe.ps1"
    path.parent.mkdir()
    path.write_text(_PROBE, encoding="utf-8")
    return path


class TestScriptCommand:
    @pytest.mark.parametrize("policy", ["bypass", "unrestricted", "remotesigned"])
    def test_a_policy_that_runs_local_files_keeps_the_plain_file_launch(self, policy: str) -> None:
        argv, extra = ps_script.script_command("x.ps1", ["launch", "-Restart"], policy=policy)
        assert argv == [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", "x.ps1", "launch", "-Restart"]
        assert extra == {}

    @pytest.mark.parametrize("policy", ["allsigned", "restricted", "undefined", None])
    def test_any_other_policy_or_an_unread_one_loads_the_script_as_text(self, policy: object) -> None:
        argv, extra = ps_script.script_command("x.ps1", ["launch", "", "a b"], policy=policy)  # type: ignore[arg-type]
        assert argv[:3] == [POWERSHELL, "-NoProfile", "-NonInteractive"] and argv[3] == "-Command"
        assert "-File" not in argv
        assert extra[ps_script.ENV_SCRIPT] == "x.ps1"
        assert extra[ps_script.ENV_ARGV].split(chr(31))[1:] == ["launch", "", "a b"]

    def test_no_launch_ever_overrides_the_policy(self) -> None:
        for policy in ("allsigned", "bypass", None):
            argv, _ = ps_script.script_command("x.ps1", [], policy=policy)
            assert "-ExecutionPolicy" not in argv

    @pytest.mark.windows_only
    @pytest.mark.parametrize("codepage, flags", [(0, NO_WINDOW), (437, 0)])
    def test_only_a_caller_with_a_console_shares_it(
        self, monkeypatch: pytest.MonkeyPatch, codepage: int, flags: int
    ) -> None:
        """A console-less caller (the tray) must never flash a window."""
        monkeypatch.setattr(ps_script.ctypes.windll.kernel32, "GetConsoleCP", lambda: codepage)
        assert ps_script._window_flags() == flags


@pytest.mark.windows_only
class TestUnderAllSigned:
    def test_the_control_a_plain_file_launch_is_refused(self, probe: Path) -> None:
        done = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "AllSigned", "-File", str(probe)],
            capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW,
        )
        assert done.returncode != 0 and "not digitally signed" in done.stderr

    def test_the_launcher_runs_it_with_args_root_and_exit_code(self, probe: Path) -> None:
        done = _launch(probe, ["launch", "-Name", "two words", "-Empty", "", "-Flag"], "AllSigned")
        assert done.returncode == 7, done.stderr
        assert f"action=launch name=[two words] empty=[] flag=True root=[{probe.parent}]" in done.stdout
        assert "execution policy allsigned" in done.stderr

    def test_a_value_that_starts_with_a_dash_stays_a_value(self, probe: Path) -> None:
        """As under -File: `-TrayLaunch "-m tray"`, or a bare `-m`, is the value of the name before it."""
        done = _launch(probe, ["launch", "-Name", "-m tray", "-Empty", "-m", "-Flag"], "AllSigned")
        assert done.returncode == 7, done.stderr
        assert "name=[-m tray] empty=[-m] flag=True" in done.stdout

    @pytest.mark.parametrize("policy", ["AllSigned", "Unrestricted"])
    def test_a_console_caller_sees_the_script_output(self, tmp_path: Path, policy: str) -> None:
        script, harness, screen = (tmp_path / n for n in ("say.ps1", "harness.py", "screen.txt"))
        script.write_text("Write-Host from-host; cmd.exe /c echo from-native", encoding="utf-8")
        harness.write_text(_CONSOLE_HARNESS, encoding="utf-8")
        hidden = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=0)
        env = {**os.environ, "PSExecutionPolicyPreference": policy, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO_ROOT)}
        subprocess.run(
            [sys.executable, str(harness), str(script), str(screen)], cwd=REPO_ROOT, env=env, timeout=150,
            check=True, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden,
        )
        shown = screen.read_text(encoding="utf-8")
        assert "from-host" in shown and "from-native" in shown, shown.strip()

    def test_under_the_default_policy_the_file_launch_is_unchanged(self, probe: Path) -> None:
        done = _launch(probe, ["launch", "-Name", "x"], "Unrestricted")
        assert done.returncode == 7, done.stderr
        assert "action=launch name=[x] empty=[] flag=False" in done.stdout
        assert "with -File" in done.stderr

    def test_a_script_error_exits_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.ps1"
        bad.write_text("throw 'boom'\n", encoding="utf-8")
        done = _launch(bad, [], "AllSigned")
        assert done.returncode == 1 and "boom" in done.stderr

    def test_the_session_host_restart_starts_under_allsigned(self) -> None:
        """Bare, without -Confirm, it only prints its warning and exits 1, so this kills nothing."""
        done = _launch(REPO_ROOT / "scripts" / "restart-session-host.ps1", [], "AllSigned")
        assert done.returncode == 1, done.stderr
        assert "Re-run with -Confirm" in done.stdout
        assert "cannot be loaded" not in done.stderr


class TestEffectivePolicy:
    def test_a_probe_that_cannot_run_is_not_confirmed_never_assumed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: object, **_k: object) -> object:
            raise OSError("no powershell")

        monkeypatch.setattr(ps_script.subprocess, "run", boom)
        assert ps_script.effective_policy() is None

    @pytest.mark.windows_only
    def test_it_reads_the_process_scope_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PSExecutionPolicyPreference", "AllSigned")
        assert ps_script.effective_policy() == "allsigned"
