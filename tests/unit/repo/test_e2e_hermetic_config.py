"""The e2e gate's webapp config is hermetic: nothing the owner set live reaches it.

The autoboot webapp used to boot from a copy of the checkout's
``config/webapp_config.json``, so an owner-side setting (the chief doorbell
switched on) leaked into tests that assume the defaults, and a worktree's copied
config carried it too. These pins run the real ``prepare_isolation`` against a
live config holding unusual values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.webapp_config import load_webapp_config
from tests._credential_hygiene import HOST_STATE_KEYS
from tests.e2e.fixtures import autoboot

_UNUSUAL_LIVE_CONFIG = {
    "chief_doorbell": True,
    "chief_job_failures": False,
    "hide_channel_sessions": False,
    "autostart_channel_sessions": True,
    "claude_model": "not-a-default-model",
    "coding_favorites": ["alpha"],
    "life_os_dir": "E:/example/life-os",
    "fleet_config_dir": "E:/example/fleet-config",
    "apps_scan_root": "E:/example/apps",
    "tunnel_url": "https://example.test/",
    "port": 9999,
}


def _isolated_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    live: Path,
) -> dict:
    monkeypatch.setattr(autoboot, "WEBAPP_CONFIG", live)
    iso = autoboot.prepare_isolation(tmp_path_factory, "tok-hermetic-12345678")
    raw = json.loads(iso.cfg_copy.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if k not in HOST_STATE_KEYS}


def test_gate_config_ignores_every_live_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    tmp_path: Path,
) -> None:
    """A live config full of unusual values gives the same gate config as no live config."""
    live = tmp_path / "webapp_config.json"
    live.write_text(json.dumps(_UNUSUAL_LIVE_CONFIG), encoding="utf-8")

    with_live = _isolated_config(monkeypatch, tmp_path_factory, live)
    without_live = _isolated_config(monkeypatch, tmp_path_factory, tmp_path / "absent.json")

    assert with_live == without_live


def test_gate_instance_reports_defaults_with_the_doorbell_on_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    tmp_path: Path,
) -> None:
    """The doorbell test's premise: off in the gate's config while the live one is on."""
    live = tmp_path / "webapp_config.json"
    live.write_text(json.dumps({"chief_doorbell": True}), encoding="utf-8")
    monkeypatch.setattr(autoboot, "WEBAPP_CONFIG", live)

    iso = autoboot.prepare_isolation(tmp_path_factory, "tok-hermetic-12345678")

    assert load_webapp_config(iso.cfg_copy).chief_doorbell is False
