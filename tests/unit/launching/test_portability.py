"""A plain ``git pull`` plus ``tray.bat --restart`` updates any PC.

Every file a session, a tray run, a restart and the gate write is gitignored (a
junctioned ``.venv`` included), no tracked launcher carries a machine's port,
and no start or restart path needs a signed script or overrides the machine's
PowerShell execution policy. The AllSigned runs use ``PSExecutionPolicyPreference``,
which sets the process-scope policy of one PowerShell tree and touches no
machine or user setting.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.launching import ps_script
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[3]
# host-identity-exempt: a guard over the live tray helper installed under this machine's real home
TRAY_HELPER = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".claude" / "tray" / "tray_lifecycle.ps1"
GIT_TIMEOUT_S = 60
PS_TIMEOUT_S = 120

# What a session, a tray run, a restart, an install and the gate write inside the
# checkout, one file each. The tracked samples beside them must stay visible.
RUNTIME_FILES = (
    "config/config.json", "config/webapp_config.json", "config/apps.json", "config/jobs.json",
    "config/channel_profiles.json", "config/process_auto_restart.json", "config/app_password.json",
    "config/webauthn_devices.json", "config/jobs.json.lock", "config/config.json.bak-20260101-120000",
    "webapp/restart.log", "webapp/startup.log", "webapp/last_tunnel_url.txt", "webapp/cloudflared.yml",
    "webapp/certificates/cert.pem", "webapp/sessions/x.json", "webapp/jobs/x/run.json", "webapp/outbox/x.txt",
    "webapp/briefs/x.md", "webapp/verify-progress.log", "webapp/spawned-apps.json", "webapp/chief-pointer.json",
    "webapp/restart-baseline.json", "webapp/terminal-themes.json", "webapp/e2e-autoboot-pids.jsonl",
    ".launcher-tmp/x.png", ".proof/x.png", "tmp/scratch.txt", "gate.log", ".tray.pid", ".pytest_cache/x",
    ".ruff_cache/x", "app/__pycache__/x.pyc", "node_modules/x/index.js",
)
SAMPLES = ("config/config.sample.json", "config/webapp_config.sample.json", "config/jobs.sample.json")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}  # a status read must never leave an index lock behind
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env,
                          timeout=GIT_TIMEOUT_S, check=True, creationflags=NO_WINDOW)
    return done.stdout


def _link_dir(link: Path, target: Path) -> None:
    """A junction on Windows (what a worktree's ``.venv`` is), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_every_runtime_file_and_a_junctioned_venv_leave_git_status_clean(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    shutil.copy(REPO_ROOT / ".gitignore", repo / ".gitignore")
    for rel in SAMPLES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, repo / rel)
    venv = tmp_path / "real-venv"
    (venv / "Scripts").mkdir(parents=True)
    _link_dir(repo / ".venv", venv)
    for rel in RUNTIME_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x", encoding="utf-8")
    listed = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    assert sorted(listed) == sorted(["?? .gitignore", *(f"?? {rel}" for rel in SAMPLES)])


def test_the_gitignore_names_the_venv_without_a_trailing_slash() -> None:
    """``.venv/`` matches a folder only; git for Windows can see a junctioned venv as a link."""
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".venv" in lines and ".venv/" not in lines


class TestNoMachinePortInLaunchers:
    """Ports come from the ignored config through ``scripts.webapp_port``; a literal is only a fallback."""

    def test_the_bats_read_the_port_through_the_resolver(self) -> None:
        for rel in ("tray.bat", "scripts/webapp.bat"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "-m scripts.webapp_port" in text, rel
        webapp = (REPO_ROOT / "scripts" / "webapp.bat").read_text(encoding="utf-8")
        assert re.findall(r"--port (\S+)", webapp) == ["%PORT%", "%PORT%"]

    def test_the_session_host_restart_reads_both_ports(self) -> None:
        text = (REPO_ROOT / "scripts" / "restart-session-host.ps1").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "scripts.webapp_port --session-host" in code
        assert not re.search(r"\b844[56]\b", code)

    def test_the_resolver_reads_this_pc_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts import webapp_port
        from src.app_config import AppConfig

        monkeypatch.setattr(webapp_port, "load_app_config", lambda: AppConfig(webapp={"port": 9123}))
        assert webapp_port.resolve_webapp_port() == 9123
        monkeypatch.setattr(webapp_port, "load_app_config", lambda: AppConfig())
        assert webapp_port.resolve_webapp_port() == 8445


def test_the_samples_carry_placeholder_paths_only() -> None:
    """setup.bat copies a sample onto a fresh PC, so a real path in one would point that PC at this one's drive."""
    placeholder = re.compile(r"^[A-Za-z]:(\\\\|/)+(path(\\\\|/)+to|Users(\\\\|/)+you)(\\\\|/)", re.I)
    found = []
    for sample in (REPO_ROOT / "config").glob("*.sample.json"):
        for match in re.finditer(r'"([A-Za-z]:[\\/][^"]*)"', sample.read_text(encoding="utf-8")):
            if not placeholder.match(match.group(1)):
                found.append(f"{sample.name}: {match.group(1)}")
    assert found == []


class TestAllSignedStartAndRestart:
    def test_no_launcher_overrides_the_policy_or_carries_a_signature(self) -> None:
        """A signature appended to a tracked script turns every ``git pull`` on an AllSigned PC into a conflict."""
        scripts = [*REPO_ROOT.glob("*.bat"), *(REPO_ROOT / "scripts").glob("*.bat"), *(REPO_ROOT / "scripts").glob("*.ps1"),
                   REPO_ROOT / "src" / "launching" / "ps_script.py", REPO_ROOT / "src" / "jobs_kinds" / "powershell.py"]
        texts = {p.name: p.read_text(encoding="utf-8") for p in scripts}
        assert [n for n, t in texts.items() if re.search(r"-ExecutionPolicy\s+Bypass|\"-ExecutionPolicy\"", t, re.I)] == []
        assert [n for n, t in texts.items() if "SIG # Begin signature block" in t] == []

    def test_no_start_or_restart_runs_a_script_from_its_file(self) -> None:
        bat = re.sub(r"(?m)^REM.*$", "", (REPO_ROOT / "tray.bat").read_text(encoding="utf-8"))
        assert not re.search(r"-File\s", bat)
        restart = (REPO_ROOT / "scripts" / "restart-session-host.ps1").read_text(encoding="utf-8")
        assert not re.search(r"^[^#]*-File\s", restart, re.MULTILINE)

    def test_docs_start_the_session_host_restart_through_the_launcher(self) -> None:
        docs = [REPO_ROOT / "README.md", REPO_ROOT / "CLAUDE.md", *(REPO_ROOT / "docs").glob("*.md")]
        offenders = [p.name for p in docs if re.search(r"-File\s+scripts[/\\]restart-session-host",
                                                        p.read_text(encoding="utf-8"))]
        assert offenders == []

    @pytest.mark.windows_only
    @pytest.mark.skipif(not TRAY_HELPER.is_file(), reason="this PC has no shared tray helper")
    def test_tray_bat_starts_the_shared_helper_under_allsigned(self, tmp_path: Path) -> None:
        """tray.bat's own -Command line, run with the read-only ``detect`` action instead of ``launch``."""
        bat = (REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
        match = re.search(r'^%PS% -NoProfile -NonInteractive -Command "(\$a = .*)"\r?$', bat, re.MULTILINE)
        assert match and " launch @a " in match.group(1)
        command = match.group(1).replace(" launch @a ", " detect @a ")
        env = {**os.environ, "PSExecutionPolicyPreference": "AllSigned", "TRAY_PS": str(TRAY_HELPER),
               "TRAY_VENV": str(tmp_path), "TRAY_APP_NAME": "probe", "SCRIPT_DIR_ARG": str(tmp_path),
               "OWNED_PORTS": "", "TRAY_LAUNCH": "scripts/launcher.py tray"}
        env.pop("WANT_RESTART", None)
        done = subprocess.run([ps_script.POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=PS_TIMEOUT_S, env=env, creationflags=NO_WINDOW)
        assert done.returncode == 0, done.stderr
        assert "cannot be loaded" not in done.stderr and done.stdout.strip() == ""


class TestLauncherLivesUnderScripts:
    """The launcher sits in ``scripts/``; the tray still finds one started from the old root spelling."""

    def test_tray_match_covers_both_spellings(self) -> None:
        bat = (REPO_ROOT / "tray.bat").read_text(encoding="utf-8")
        match = re.search(r"TrayMatch = '([^']+)'", bat)
        assert match
        pattern = re.compile(match.group(1))
        for cmdline in ("pythonw.exe launcher.py tray", r"pythonw.exe scripts\launcher.py tray",
                        "pythonw.exe scripts/launcher.py tray"):
            assert pattern.search(cmdline), cmdline
        assert not pattern.search("pythonw.exe launcher.py webapp")
        assert r"TRAY_LAUNCH=scripts\launcher.py tray" in bat

    def test_the_root_holds_only_the_two_day_to_day_batch_files(self) -> None:
        assert sorted(p.name for p in REPO_ROOT.glob("*.bat")) == ["setup.bat", "tray.bat"]
        assert not (REPO_ROOT / "launcher.py").exists()

    def test_a_bad_command_prints_the_usage_line(self) -> None:
        done = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "launcher.py"), "no-such-command"],
                              capture_output=True, text=True, timeout=PS_TIMEOUT_S, creationflags=NO_WINDOW)
        assert done.returncode != 0
        assert "usage: launcher" in done.stderr
