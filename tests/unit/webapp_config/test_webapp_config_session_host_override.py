"""Regression for #260 — the session-host port env override.

The e2e pre-ship gate must be able to point its disposable webapp at a
disposable, free-port session-host instead of adopting the live :8446 a
running tray owns (which holds the user's real PTY/Claude sessions — the
gate used to kill them). That isolation hinges on
``LAUNCHER_SESSION_HOST_PORT`` being honoured by ``load_webapp_config``.
These tests lock that primitive so the isolation can't silently regress.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.webapp_config import (
    DEFAULT_SESSION_HOST_PORT,
    SESSION_HOST_PORT_ENV,
    load_webapp_config,
)


def _write_cfg(tmp_path: Path, **overrides) -> Path:
    cfg = {"host": "127.0.0.1", "port": 8445, "session_host_port": 8446}
    cfg.update(overrides)
    target = tmp_path / "webapp_config.json"
    target.write_text(json.dumps(cfg), encoding="utf-8")
    return target


def test_no_env_uses_configured_port(tmp_path, monkeypatch):
    monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
    cfg = load_webapp_config(_write_cfg(tmp_path))
    assert cfg.session_host_port == 8446


def test_env_override_applied(tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_HOST_PORT_ENV, "53999")
    cfg = load_webapp_config(_write_cfg(tmp_path))
    assert cfg.session_host_port == 53999


def test_env_override_applies_to_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_HOST_PORT_ENV, "54001")
    cfg = load_webapp_config(tmp_path / "does-not-exist.json")
    assert cfg.session_host_port == 54001


@pytest.mark.parametrize("bad", ["", "   ", "not-a-port", "0", "70000", "-1"])
def test_invalid_env_ignored(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(SESSION_HOST_PORT_ENV, bad)
    cfg = load_webapp_config(_write_cfg(tmp_path))
    assert cfg.session_host_port == 8446


def test_override_must_differ_from_webapp_port(tmp_path, monkeypatch):
    # _validate rejects session_host_port == webapp port; the override is
    # applied before validation, so a colliding value must still be caught.
    monkeypatch.setenv(SESSION_HOST_PORT_ENV, "8445")
    with pytest.raises(ValueError):
        load_webapp_config(_write_cfg(tmp_path, port=8445))


def test_default_constant_unchanged():
    # The live tray still defaults to :8446 — only the gate overrides it.
    assert DEFAULT_SESSION_HOST_PORT == 8446
