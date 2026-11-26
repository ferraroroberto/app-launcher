"""A save must never replace a config the load could not read with defaults.

Runs only on files under ``tmp_path``, never the live config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.webapp_config import (
    SESSION_HOST_PORT_ENV,
    WEBAPP_CONFIG_PATH_ENV,
    WebappConfig,
    load_webapp_config,
    save_webapp_config,
    update_webapp_config,
)

_BOM = b"\xef\xbb\xbf"
_OWNER = {"terminal_history_lines": 4321, "auth_token": "synthetic-token"}


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "webapp_config.json"
    monkeypatch.setenv(WEBAPP_CONFIG_PATH_ENV, str(target))
    monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
    return target


def test_bom_file_loads_its_own_values(config_file: Path) -> None:
    config_file.write_bytes(_BOM + json.dumps(_OWNER).encode("utf-8"))
    cfg = load_webapp_config()
    assert (cfg.terminal_history_lines, cfg.auth_token) == (4321, "synthetic-token")


def test_save_on_bom_file_keeps_unchanged_fields(config_file: Path) -> None:
    config_file.write_bytes(_BOM + json.dumps(_OWNER).encode("utf-8"))
    update_webapp_config(chief_worker_cap=3)
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk["chief_worker_cap"] == 3
    assert {key: on_disk[key] for key in _OWNER} == _OWNER


@pytest.mark.parametrize(
    "content",
    [b'{"auth_token": "synthetic-token",', b"[1, 2]", '{"port": 1}'.encode("utf-16")],
    ids=["truncated", "not-an-object", "utf-16"],
)
def test_save_over_unreadable_file_is_refused(config_file: Path, content: bytes) -> None:
    config_file.write_bytes(content)
    with pytest.raises(ValueError, match="cannot be read"):
        save_webapp_config(WebappConfig())
    assert config_file.read_bytes() == content


def test_update_over_malformed_file_is_refused(config_file: Path) -> None:
    content = b"{not json"
    config_file.write_bytes(content)
    with pytest.raises(ValueError, match="cannot be read"):
        update_webapp_config(chief_worker_cap=3)
    assert config_file.read_bytes() == content
