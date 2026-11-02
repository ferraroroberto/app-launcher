"""Fixtures for the /api/config tests: a temp Startup folder and a faked remote
power guard for every test here."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.boot_autostart as boot_autostart_mod
import src.remote_power_guard as guard_mod


@pytest.fixture(autouse=True)
def _isolated_startup_dir(tmp_path: Path, monkeypatch) -> Path:
    """Point boot_autostart's Startup folder at a tmp dir for every test in
    this module — the real per-user Startup folder must never be touched
    by a test run."""
    startup_dir = tmp_path / "Startup"
    monkeypatch.setattr(boot_autostart_mod, "_startup_dir", lambda: startup_dir)
    return startup_dir


@pytest.fixture(autouse=True)
def fake_guard(monkeypatch) -> dict:
    """The remote power guard with Task Scheduler and the registry faked:
    ``state`` is what is_enabled() reports, ``elevated`` the token check."""
    fake = {"enabled": False, "no_close": 0, "elevated": False, "calls": []}

    def enable() -> None:
        fake["calls"].append("enable")
        if not fake["elevated"]:
            raise guard_mod.ElevationRequired("needs elevation")
        fake["enabled"] = True

    def disable() -> bool:
        fake["calls"].append("disable")
        if not fake["elevated"]:
            raise guard_mod.ElevationRequired("needs elevation")
        fake["enabled"], fake["no_close"] = False, 0
        return True

    monkeypatch.setattr(guard_mod, "is_enabled", lambda: fake["enabled"])
    monkeypatch.setattr(guard_mod, "read_no_close", lambda: fake["no_close"])
    monkeypatch.setattr(guard_mod, "enable", enable)
    monkeypatch.setattr(guard_mod, "disable", disable)
    return fake
