"""The general bot's two pre-approved router commands in its channel settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.launching import channel_profiles
from src.launching.channel_profiles import write_channel_settings


def _state_dir(tmp_path: Path, name: str) -> str:
    d = tmp_path / name
    d.mkdir()
    return d.as_posix()


class TestRouterGrant:
    def test_general_profile_allows_exactly_the_two_router_commands(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
        root = tmp_path / "life-os"
        root.mkdir()
        script = (
            root.resolve() / ".claude" / "skills" / "general" / "scripts"
            / "router_tools.py"
        ).as_posix()
        state = _state_dir(tmp_path, "g")
        general = channel_profiles.ChannelProfile("general", "General", "general", state)
        settings = json.loads(Path(
            write_channel_settings(general, life_os_dir=root)
        ).read_text(encoding="utf-8"))
        assert settings["permissions"] == {"allow": [
            f"Bash(python {script} skills)",
            f"Bash(python {script} resume --skill:*)",
        ]}
        assert settings["env"] == {
            "TELEGRAM_STATE_DIR": state, "FLEET_CONTEXT_FILTER_MODE": "off",
        }

    def test_other_profiles_are_unchanged_by_the_router_grant(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
        root = tmp_path / "life-os"
        root.mkdir()
        state = _state_dir(tmp_path, "h")
        health = channel_profiles.ChannelProfile("health", "Health", "health", state)
        assert json.loads(Path(
            write_channel_settings(health, life_os_dir=root)
        ).read_text(encoding="utf-8")) == {"env": {"TELEGRAM_STATE_DIR": state}}

    def test_general_profile_without_a_life_os_dir_gets_no_grant(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
        state = _state_dir(tmp_path, "g")
        general = channel_profiles.ChannelProfile("general", "General", "general", state)
        assert json.loads(Path(write_channel_settings(general)).read_text(
            encoding="utf-8"
        )) == {"env": {"TELEGRAM_STATE_DIR": state}}

    def test_router_rules_refuse_a_life_os_path_with_a_space(self, tmp_path):
        spaced = tmp_path / "life os"
        spaced.mkdir()
        with pytest.raises(ValueError, match="space"):
            channel_profiles.router_allow_rules(spaced)
