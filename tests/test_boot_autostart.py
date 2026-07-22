"""Tests for src.boot_autostart (issue #456, part 1/2).

The Startup-folder wrapper bat is the mechanism actually shipped — see the
module docstring for why the originally-planned `schtasks /Create /SC
ONLOGON` route was reverted (empirically confirmed Access Denied from an
unelevated process). These tests point `startup_dir` at a tmp_path so no
assertion ever touches the real per-user Startup folder.
"""

from __future__ import annotations

from pathlib import Path

import src.boot_autostart as boot_autostart


def test_disabled_by_default(tmp_path: Path):
    assert boot_autostart.is_enabled(tmp_path) is False


def test_enable_writes_wrapper_bat_pointing_at_tray_bat(tmp_path: Path):
    startup_dir = tmp_path / "Startup"
    tray_bat = tmp_path / "repo" / "tray.bat"
    tray_bat.parent.mkdir(parents=True)
    tray_bat.write_text("@echo off\r\n", encoding="utf-8")

    path = boot_autostart.enable(tray_bat=tray_bat, startup_dir=startup_dir)

    assert path == startup_dir / boot_autostart.STARTUP_BAT_NAME
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert str(tray_bat.parent) in content
    assert str(tray_bat) in content
    assert boot_autostart.is_enabled(startup_dir) is True


def test_wrapper_bat_has_breadcrumb_and_output_redirection(tmp_path: Path):
    # A login-time failure was silent before #582: the wrapper must leave a
    # timestamped trail and redirect tray.bat's output on every attempt.
    startup_dir = tmp_path / "Startup"
    tray_bat = tmp_path / "repo" / "tray.bat"
    tray_bat.parent.mkdir(parents=True)
    tray_bat.write_text("@echo off\r\n", encoding="utf-8")

    path = boot_autostart.enable(tray_bat=tray_bat, startup_dir=startup_dir)
    content = path.read_text(encoding="utf-8")

    log = str(boot_autostart._startup_log_path(tray_bat.parent))
    # Timestamped breadcrumb line naming the wrapper firing.
    assert "autostart wrapper fired" in content
    assert "%date% %time%" in content
    # tray.bat's own output is captured (stdout+stderr) to the startup log.
    assert f'call "{tray_bat}" >>"{log}" 2>&1' in content
    # The exit code is recorded so a silent death becomes a logged non-zero.
    assert "tray.bat returned errorlevel %ERRORLEVEL%" in content
    # The one hard precondition (missing shared helper) is named, not left as
    # a bare non-zero exit.
    assert "precondition missing" in content
    assert r"%USERPROFILE%\.claude\tray\tray_lifecycle.ps1" in content


def test_enable_verifies_write_landed(tmp_path: Path, monkeypatch):
    # enable() must surface a failed/partial write instead of silently
    # succeeding (the #582 failure mode was a silent no-op).
    startup_dir = tmp_path / "Startup"
    tray_bat = tmp_path / "repo" / "tray.bat"
    tray_bat.parent.mkdir(parents=True)
    tray_bat.write_text("@echo off\r\n", encoding="utf-8")

    real_read_bytes = Path.read_bytes

    def corrupt_read_bytes(self, *args, **kwargs):
        if self.name == boot_autostart.STARTUP_BAT_NAME:
            return b"TRUNCATED"
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", corrupt_read_bytes)

    try:
        boot_autostart.enable(tray_bat=tray_bat, startup_dir=startup_dir)
    except OSError as exc:
        assert "did not land intact" in str(exc)
    else:
        raise AssertionError("expected OSError when the read-back does not match")


def test_enable_creates_missing_startup_dir(tmp_path: Path):
    startup_dir = tmp_path / "does" / "not" / "exist" / "yet"
    tray_bat = tmp_path / "repo" / "tray.bat"
    tray_bat.parent.mkdir(parents=True)
    tray_bat.write_text("@echo off\r\n", encoding="utf-8")

    boot_autostart.enable(tray_bat=tray_bat, startup_dir=startup_dir)

    assert startup_dir.is_dir()
    assert (startup_dir / boot_autostart.STARTUP_BAT_NAME).is_file()


def test_disable_removes_wrapper_bat(tmp_path: Path):
    startup_dir = tmp_path / "Startup"
    tray_bat = tmp_path / "repo" / "tray.bat"
    tray_bat.parent.mkdir(parents=True)
    tray_bat.write_text("@echo off\r\n", encoding="utf-8")
    boot_autostart.enable(tray_bat=tray_bat, startup_dir=startup_dir)

    removed = boot_autostart.disable(startup_dir)

    assert removed is True
    assert boot_autostart.is_enabled(startup_dir) is False


def test_disable_when_never_enabled_is_a_noop(tmp_path: Path):
    startup_dir = tmp_path / "Startup"
    startup_dir.mkdir()

    removed = boot_autostart.disable(startup_dir)

    assert removed is False


def test_startup_dir_requires_appdata_env(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    try:
        boot_autostart._startup_dir()
    except RuntimeError as exc:
        assert "APPDATA" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when APPDATA is unset")
