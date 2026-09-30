"""Telegram channel launch profiles (#1366): loader, flag builder, launch route."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import channel_profiles
from src.channel_profiles import (
    label_profile_id,
    load_channel_profiles,
    profile_session_label,
    write_channel_settings,
)
from src.launch_flags import CHANNEL_PLUGIN, build_claude_flags
from src.webapp_config import WebappConfig
from tests.test_webapp_api_life_os import life_os_client  # noqa: F401  (fixture)


def _write_profiles(path: Path, rows: list) -> Path:
    path.write_text(json.dumps({"profiles": rows}), encoding="utf-8")
    return path


def _state_dir(tmp_path: Path, name: str) -> str:
    d = tmp_path / name
    d.mkdir()
    return d.as_posix()


# ------------------------------------------------------------ flag builder
class TestChannelFlags:
    def test_default_output_is_unchanged(self):
        """Sessions without a profile launch exactly as before (AC 4)."""
        cfg = WebappConfig()
        assert build_claude_flags(cfg) == (
            "--remote-control --permission-mode auto --model "
            f"{cfg.claude_model}"
            + (f" --effort {cfg.claude_effort}" if cfg.claude_effort != "off" else "")
            + (" --verbose" if cfg.claude_verbose else "")
            + (" --debug" if cfg.claude_debug else "")
        )
        assert "--channels" not in build_claude_flags(cfg, model_override="opus")
        assert "--settings" not in build_claude_flags(cfg)

    def test_channel_appends_plugin_and_settings(self):
        cfg = WebappConfig()
        plain = build_claude_flags(cfg, model_override="sonnet")
        flags = build_claude_flags(
            cfg, model_override="sonnet", channel_settings="C:/x/health.json"
        )
        assert flags == (
            f"{plain} --channels {CHANNEL_PLUGIN} --settings C:/x/health.json"
        )

    def test_channel_session_never_bypasses_permissions(self):
        cfg = WebappConfig()
        cfg.claude_permission_mode = "skip"
        assert "--dangerously-skip-permissions" in build_claude_flags(cfg)
        flags = build_claude_flags(cfg, channel_settings="C:/x/health.json")
        assert "--dangerously-skip-permissions" not in flags
        assert "--permission-mode auto" in flags


# ------------------------------------------------------------------ loader
class TestLoadProfiles:
    def test_missing_file_is_no_profiles_no_problems(self, tmp_path):
        assert load_channel_profiles(tmp_path / "nope.json") == ([], [])

    def test_sample_parses_shape(self, tmp_path, project_root):
        sample = json.loads(
            (project_root / "config" / "channel_profiles.sample.json").read_text(
                encoding="utf-8"
            )
        )
        for row in sample["profiles"]:
            assert set(row) == {"id", "label", "skill", "state_dir"}
            # Only paths and names: nothing that could be a credential.
            assert "token" not in json.dumps(row).lower()

    def test_valid_profiles_load(self, tmp_path):
        path = _write_profiles(tmp_path / "p.json", [
            {"id": "health", "label": "Health", "skill": "health",
             "state_dir": _state_dir(tmp_path, "h")},
            {"id": "school", "skill": "school",
             "state_dir": _state_dir(tmp_path, "s")},
        ])
        profiles, problems = load_channel_profiles(path)
        assert problems == []
        assert [p.id for p in profiles] == ["health", "school"]
        assert profiles[1].label == "School"  # derived from the id

    def test_bad_rows_are_skipped_with_a_reason(self, tmp_path):
        good = _state_dir(tmp_path, "g")
        path = _write_profiles(tmp_path / "p.json", [
            {"id": "Bad Id", "skill": "x", "state_dir": good},
            {"id": "nodir", "skill": "x", "state_dir": str(tmp_path / "missing")},
            {"id": "rel", "skill": "x", "state_dir": "relative/dir"},
            {"id": "noskill", "state_dir": good},
            {"id": "ok", "skill": "x", "state_dir": good},
            {"id": "ok", "skill": "x", "state_dir": good},
            {"id": "sharedir", "skill": "x", "state_dir": good},
        ])
        profiles, problems = load_channel_profiles(path)
        assert [p.id for p in profiles] == ["ok"]
        joined = "\n".join(problems)
        assert "must be lowercase" in joined
        assert "does not exist" in joined
        assert "absolute path" in joined
        assert "skill is required" in joined
        assert "duplicate id" in joined
        # Two bots on one state dir would share a token → 409.
        assert "already used by 'ok'" in joined

    def test_malformed_file_reports_and_does_not_raise(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text("{nope", encoding="utf-8")
        profiles, problems = load_channel_profiles(path)
        assert profiles == [] and len(problems) == 1

    def test_label_roundtrip(self):
        assert label_profile_id(profile_session_label("health")) == "health"
        assert label_profile_id("chief") == ""
        assert label_profile_id(None) == ""


class TestSettingsFile:
    def test_carries_only_the_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
        state = _state_dir(tmp_path, "h")
        profile = channel_profiles.ChannelProfile("health", "Health", "health", state)
        path = write_channel_settings(profile)
        assert json.loads(Path(path).read_text(encoding="utf-8")) == {
            "env": {"TELEGRAM_STATE_DIR": state}
        }
        # Survives the session-host's flag allowlist + whitespace tokenizing.
        assert " " not in path and "\\" not in path

    def test_refuses_a_path_that_cannot_ride_a_launch_command(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(
            "APP_LAUNCHER_DATA_DIR", (tmp_path / "with space").as_posix()
        )
        profile = channel_profiles.ChannelProfile(
            "health", "Health", "health", _state_dir(tmp_path, "h")
        )
        with pytest.raises(ValueError, match="APP_LAUNCHER_DATA_DIR"):
            write_channel_settings(profile)


# ------------------------------------------------------------------ routes
class TestChannelRoutes:
    @pytest.fixture(autouse=True)
    def _bypass_gate(self, monkeypatch):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware,
            "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )

    @pytest.fixture
    def channels(self, life_os_client, tmp_path, monkeypatch):  # noqa: F811
        """A client with two profiles (one skill exists) and a recording spawn."""
        client, app, _ = life_os_client
        monkeypatch.setenv("APP_LAUNCHER_DATA_DIR", (tmp_path / "data").as_posix())
        health = _state_dir(tmp_path, "tg-health")
        school = _state_dir(tmp_path, "tg-school")
        app.state.channel_profiles_path = _write_profiles(tmp_path / "p.json", [
            {"id": "health", "label": "Health", "skill": "journal-daily",
             "state_dir": health},
            {"id": "school", "label": "School", "skill": "journal-daily",
             "state_dir": school},
        ])
        from app.webapp.routers import life_os_channels, life_os_spawn
        spawned: list = []

        def fake_spawn(project_dir, name, flags, port, kind, agent,
                       rows=40, cols=120, history_lines=None, label=""):
            spawned.append(SimpleNamespace(flags=flags, kind=kind, agent=agent,
                                           label=label))
            sid = f"sid-{len(spawned)}"
            live.append({"session_id": sid, "label": label, "name": name,
                         "alive": True})
            return {"session_id": sid, "kind": kind}

        live: list = []
        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
        life_os_channels.session_client.list_sessions.side_effect = (
            lambda port: list(live)
        )
        return SimpleNamespace(client=client, spawned=spawned, live=live,
                               health=health, school=school)

    def test_launch_starts_skill_with_channel_flags_and_label(self, channels):
        resp = channels.client.post(
            "/api/life-os/channels/health/launch", json={"model": "sonnet"}
        )
        assert resp.status_code == 200, resp.text
        (spawn,) = channels.spawned
        assert spawn.label == "telegram:health"
        assert spawn.agent == "claude" and spawn.kind == "pty"
        assert f"--channels {CHANNEL_PLUGIN}" in spawn.flags
        assert "--settings " in spawn.flags
        assert spawn.flags.split()[-1] == "/journal-daily"
        settings = spawn.flags.split("--settings ")[1].split()[0]
        assert json.loads(Path(settings).read_text(encoding="utf-8")) == {
            "env": {"TELEGRAM_STATE_DIR": channels.health}
        }

    def test_resume_keeps_channel_flag_and_state_dir(self, channels):
        resp = channels.client.post(
            "/api/life-os/channels/school/launch",
            json={"model": "sonnet", "resume": True},
        )
        assert resp.status_code == 200, resp.text
        (spawn,) = channels.spawned
        assert spawn.flags.split()[-1] == "/resume"
        assert f"--channels {CHANNEL_PLUGIN}" in spawn.flags
        assert spawn.label == "telegram:school"
        settings = spawn.flags.split("--settings ")[1].split()[0]
        assert json.loads(Path(settings).read_text(encoding="utf-8"))["env"] == {
            "TELEGRAM_STATE_DIR": channels.school
        }

    def test_second_launch_of_a_running_profile_is_refused(self, channels):
        first = channels.client.post("/api/life-os/channels/health/launch", json={})
        assert first.status_code == 200
        second = channels.client.post("/api/life-os/channels/health/launch", json={})
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
        assert "sid-1" in second.json()["detail"]
        assert len(channels.spawned) == 1  # nothing was spawned the second time

    def test_a_stopped_session_frees_the_profile(self, channels):
        channels.client.post("/api/life-os/channels/health/launch", json={})
        channels.live[0]["alive"] = False
        again = channels.client.post("/api/life-os/channels/health/launch", json={})
        assert again.status_code == 200

    def test_different_profiles_run_side_by_side(self, channels):
        a = channels.client.post("/api/life-os/channels/health/launch", json={})
        b = channels.client.post("/api/life-os/channels/school/launch", json={})
        assert a.status_code == b.status_code == 200
        assert [s.label for s in channels.spawned] == [
            "telegram:health", "telegram:school"
        ]

    def test_host_unreachable_is_not_read_as_nothing_running(self, channels):
        from app.webapp.routers import life_os_channels
        err = life_os_channels.session_client.SessionHostError
        life_os_channels.session_client.list_sessions.side_effect = err("down")
        resp = channels.client.post("/api/life-os/channels/health/launch", json={})
        assert resp.status_code == 503
        assert channels.spawned == []

    def test_refusals(self, channels):
        post = channels.client.post
        assert post("/api/life-os/channels/nope/launch", json={}).status_code == 404
        assert post(
            "/api/life-os/channels/health/launch", json={"mode": "remote"}
        ).status_code == 400
        assert post(
            "/api/life-os/channels/health/launch", json={"model": "codex:gpt-5"}
        ).status_code == 400
        assert channels.spawned == []

    def test_list_reports_running_and_never_the_state_dir(self, channels):
        channels.client.post("/api/life-os/channels/health/launch", json={})
        body = channels.client.get("/api/life-os/channels").json()
        rows = {p["id"]: p for p in body["profiles"]}
        assert rows["health"]["running"] is True
        assert rows["health"]["session_id"] == "sid-1"
        assert rows["school"]["running"] is False
        assert rows["health"]["skill_found"] is True
        assert "tg-health" not in json.dumps(body)

    def test_list_marks_running_unknown_when_host_is_down(self, channels):
        from app.webapp.routers import life_os_channels
        err = life_os_channels.session_client.SessionHostError
        life_os_channels.session_client.list_sessions.side_effect = err("down")
        rows = channels.client.get("/api/life-os/channels").json()["profiles"]
        assert all(p["running"] is None for p in rows)
