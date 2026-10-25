"""Fixtures for the /api/config tests: a temp Startup folder for every test here."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.boot_autostart as boot_autostart_mod


@pytest.fixture(autouse=True)
def _isolated_startup_dir(tmp_path: Path, monkeypatch) -> Path:
    """Point boot_autostart's Startup folder at a tmp dir for every test in
    this module — the real per-user Startup folder must never be touched
    by a test run."""
    startup_dir = tmp_path / "Startup"
    monkeypatch.setattr(boot_autostart_mod, "_startup_dir", lambda: startup_dir)
    return startup_dir
