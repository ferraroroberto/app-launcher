"""Regression for #441 — the config file *path* env override.

The e2e pre-ship gate must be able to point its disposable webapp at a temp
COPY of the config so a Settings-tab e2e Save can never mutate the user's
real ``config/webapp_config.json`` (the #438 port corruption was that
shared-file design biting). That isolation hinges on
``LAUNCHER_WEBAPP_CONFIG`` being honoured symmetrically by
``load_webapp_config`` AND ``save_webapp_config``. These tests lock the
primitive so the isolation can't silently regress.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.webapp_config as webapp_cfg_mod
from src.webapp_config import (
    SESSION_HOST_PORT_ENV,
    WEBAPP_CONFIG_PATH_ENV,
    WebappConfig,
    load_webapp_config,
    save_webapp_config,
    update_webapp_config,
)


@pytest.fixture(autouse=True)
def _isolated_default(tmp_path: Path, monkeypatch) -> Path:
    """Point DEFAULT_CONFIG_PATH at a tmp file and clear both env overrides,
    so no assertion in this module can ever touch the real repo config."""
    default = tmp_path / "default" / "webapp_config.json"
    monkeypatch.setattr(webapp_cfg_mod, "DEFAULT_CONFIG_PATH", default)
    monkeypatch.delenv(WEBAPP_CONFIG_PATH_ENV, raising=False)
    monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
    return default


def _write_cfg(target: Path, **overrides) -> Path:
    cfg = {"host": "127.0.0.1", "port": 8445, "session_host_port": 8446}
    cfg.update(overrides)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg), encoding="utf-8")
    return target


def test_no_env_loads_from_default(_isolated_default, monkeypatch):
    _write_cfg(_isolated_default, terminal_history_lines=4321)
    cfg = load_webapp_config()
    assert cfg.terminal_history_lines == 4321


def test_env_override_redirects_load(_isolated_default, tmp_path, monkeypatch):
    _write_cfg(_isolated_default, terminal_history_lines=4321)
    other = _write_cfg(tmp_path / "copy.json", terminal_history_lines=7777)
    monkeypatch.setenv(WEBAPP_CONFIG_PATH_ENV, str(other))
    cfg = load_webapp_config()
    assert cfg.terminal_history_lines == 7777


def test_explicit_path_beats_env(_isolated_default, tmp_path, monkeypatch):
    explicit = _write_cfg(tmp_path / "explicit.json", terminal_history_lines=6543)
    other = _write_cfg(tmp_path / "copy.json", terminal_history_lines=7777)
    monkeypatch.setenv(WEBAPP_CONFIG_PATH_ENV, str(other))
    cfg = load_webapp_config(explicit)
    assert cfg.terminal_history_lines == 6543


def test_env_override_redirects_save(_isolated_default, tmp_path, monkeypatch):
    copy = tmp_path / "copy.json"
    monkeypatch.setenv(WEBAPP_CONFIG_PATH_ENV, str(copy))
    save_webapp_config(WebappConfig())
    assert copy.exists()
    assert not _isolated_default.exists()


def test_update_writes_env_path_never_default(
    _isolated_default, tmp_path, monkeypatch
):
    """The e2e-gate scenario end to end: with the env override set, a config
    PATCH (update_webapp_config) reads AND writes only the temp copy — the
    default (standing in for the user's real file) is never created or
    touched."""
    copy = _write_cfg(tmp_path / "copy.json")
    monkeypatch.setenv(WEBAPP_CONFIG_PATH_ENV, str(copy))
    updated = update_webapp_config(terminal_history_lines=5000)
    assert updated.terminal_history_lines == 5000
    on_disk = json.loads(copy.read_text(encoding="utf-8"))
    assert on_disk["terminal_history_lines"] == 5000
    assert not _isolated_default.exists()


def test_save_load_round_trips_terminal_history_lines(
    _isolated_default,
):
    """The #441 core: the field survives a save → fresh-load cycle (it was
    missing from both the loader's constructor call and the saver's payload,
    so a Settings save silently reverted on restart)."""
    save_webapp_config(WebappConfig(terminal_history_lines=5000))
    on_disk = json.loads(_isolated_default.read_text(encoding="utf-8"))
    assert on_disk["terminal_history_lines"] == 5000
    assert load_webapp_config().terminal_history_lines == 5000
