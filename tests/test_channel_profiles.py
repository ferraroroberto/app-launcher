"""Telegram channel launch profiles (#1366): loader, flag builder, launch route."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.launching import channel_profiles
from src.launching.channel_profiles import (
    label_profile_id,
    load_channel_profiles,
    profile_session_label,
    write_channel_settings,
)
from src.launching.flags import CHANNEL_PLUGIN, build_claude_flags
from src.webapp_config import WebappConfig


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
            + " --effort medium"  # Opus's own default (#1711)
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
@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware,
        "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


@pytest.fixture
def channels(_bypass_gate, life_os_client, tmp_path, monkeypatch):
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
                     "alive": True, "started_at": time.time()})
        return {"session_id": sid, "kind": kind}

    live: list = []
    monkeypatch.setattr(life_os_spawn, "spawn_claude_session", fake_spawn)
    life_os_channels.session_client.list_sessions.side_effect = (
        lambda port: list(live)
    )
    return SimpleNamespace(client=client, app=app, spawned=spawned,
                           live=live, health=health, school=school)


class TestChannelRoutes:
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

    def test_launch_with_no_model_uses_the_channel_model(self, channels):
        channels.app.state.webapp_config.claude_model = "opus"
        resp = channels.client.post("/api/life-os/channels/health/launch", json={})
        assert resp.status_code == 200, resp.text
        (spawn,) = channels.spawned
        assert "--model sonnet" in spawn.flags
        assert "--model opus" not in spawn.flags

    def test_launch_with_no_model_follows_a_configured_channel_model(self, channels):
        channels.app.state.webapp_config.channel_model = "opus"
        resp = channels.client.post("/api/life-os/channels/health/launch", json={})
        assert resp.status_code == 200, resp.text
        (spawn,) = channels.spawned
        assert "--model opus" in spawn.flags

    def test_launch_with_an_explicit_model_uses_it(self, channels):
        resp = channels.client.post(
            "/api/life-os/channels/health/launch", json={"model": "opus"}
        )
        assert resp.status_code == 200, resp.text
        (spawn,) = channels.spawned
        assert "--model opus" in spawn.flags

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


class TestChannelRestart:
    """Restart (#1675): quit, confirmed gone, one fresh start, under the
    profile's lock. A fake session-host only: no real session is touched."""

    @pytest.fixture
    def host(self, channels, monkeypatch):
        """The fake host's stop/get on ``channels.live``, one running session
        on ``health``, and every call recorded in order."""
        from app.webapp.routers import life_os_channels, life_os_spawn
        client_mod = life_os_channels.session_client
        monkeypatch.setattr(life_os_channels, "_launch_locks", {})
        monkeypatch.setattr(life_os_channels, "RESTART_QUIT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(life_os_channels, "RESTART_KILL_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(life_os_channels, "RESTART_POLL_SECONDS", 0.01)
        events: list = []
        state = SimpleNamespace(events=events, dies_on={"quit"}, get_error=None)
        monkeypatch.setattr(
            life_os_channels, "close_mirror_window",
            lambda sid: events.append(("mirror-close", sid)) or True,
        )

        def find(sid):
            return next((s for s in channels.live if s["session_id"] == sid), None)

        def stop(port, sid, mode="quit"):
            events.append(("stop", mode, sid))
            if mode in state.dies_on and find(sid):
                find(sid)["alive"] = False
            return {"stopped": True}

        def get_session(port, sid):
            events.append(("get", sid))
            if state.get_error is not None:
                raise state.get_error
            sess = find(sid)
            if sess is None:
                raise client_mod.SessionHostError("unknown session", status=404)
            return dict(sess)

        client_mod.stop.side_effect = stop
        client_mod.get_session.side_effect = get_session
        spawn = life_os_spawn.spawn_claude_session

        def recording_spawn(*args, **kwargs):
            events.append(("spawn",))
            return spawn(*args, **kwargs)

        monkeypatch.setattr(life_os_spawn, "spawn_claude_session", recording_spawn)
        channels.live.append({"session_id": "old-1", "label": "telegram:health",
                              "name": "journal-daily", "alive": True})
        state.channels = channels
        state.lock = lambda: life_os_channels._launch_locks.get("health")
        return state

    def _restart(self, host):
        return host.channels.client.post(
            "/api/life-os/channels/health/restart", json={}
        )

    def test_quit_then_confirmed_gone_then_one_start(self, host):
        resp = self._restart(host)
        assert resp.status_code == 200, resp.text
        assert resp.json()["restarted"] is True
        kinds = [e[0] for e in host.events]
        assert kinds[:2] == ["mirror-close", "stop"]
        assert host.events[1] == ("stop", "quit", "old-1")
        assert kinds.index("get") < kinds.index("spawn")
        assert kinds.count("spawn") == 1 and kinds[-1] == "spawn"
        (spawn,) = host.channels.spawned
        assert spawn.label == "telegram:health"
        from app.webapp.routers import life_os_channels
        (call,) = [c for c in life_os_channels.audit.audit_event.call_args_list
                   if c.args[:1] == ("channel_restart",)]
        assert call.kwargs["old_session"] == "old-1"
        assert call.kwargs["session"] == "sid-1"

    def test_restart_uses_the_channel_model_whatever_the_body_says(self, host):
        host.channels.app.state.webapp_config.claude_model = "opus"
        resp = host.channels.client.post(
            "/api/life-os/channels/health/restart", json={"model": "opus"}
        )
        assert resp.status_code == 200, resp.text
        (spawn,) = host.channels.spawned
        assert "--model sonnet" in spawn.flags

    def test_a_rewritten_skill_is_read_fresh_not_resumed(self, host):
        """The acceptance check: after a SKILL.md change, Restart's launch is
        ``/<command>`` with no resume, so Claude reads the current file."""
        life_os = Path(host.channels.app.state.webapp_config.life_os_dir)
        skill_md = life_os / ".claude" / "skills" / "journal-daily" / "SKILL.md"
        skill_md.write_text(
            skill_md.read_text(encoding="utf-8") + "\nA new step.\n",
            encoding="utf-8",
        )
        assert self._restart(host).status_code == 200
        (spawn,) = host.channels.spawned
        assert spawn.flags.split()[-1] == "/journal-daily"
        assert "--resume" not in spawn.flags and "/resume" not in spawn.flags

    def test_a_quit_that_does_not_land_escalates_to_one_kill(self, host):
        host.dies_on = {"kill"}
        resp = self._restart(host)
        assert resp.status_code == 200, resp.text
        stops = [e[1] for e in host.events if e[0] == "stop"]
        assert stops == ["quit", "kill"]
        assert len(host.channels.spawned) == 1

    def test_still_alive_after_the_kill_is_a_502_and_nothing_starts(self, host):
        host.dies_on = set()
        resp = self._restart(host)
        assert resp.status_code == 502
        assert "not started again" in resp.json()["detail"]
        assert host.channels.spawned == []
        assert [e[1] for e in host.events if e[0] == "stop"] == ["quit", "kill"]

    def test_host_unreachable_at_the_check_is_a_503_and_nothing_stops(self, host):
        from app.webapp.routers import life_os_channels
        err = life_os_channels.session_client.SessionHostError
        life_os_channels.session_client.list_sessions.side_effect = err("down")
        resp = self._restart(host)
        assert resp.status_code == 503
        assert host.events == []
        assert host.channels.spawned == []

    def test_host_lost_while_polling_is_a_502_and_nothing_starts(self, host):
        from app.webapp.routers import life_os_channels
        host.dies_on = set()
        host.get_error = life_os_channels.session_client.SessionHostError(
            "session-host unreachable", status=503
        )
        resp = self._restart(host)
        assert resp.status_code == 502
        assert "stopped answering" in resp.json()["detail"]
        assert host.channels.spawned == []

    def test_restarting_a_stopped_profile_starts_it_once(self, host):
        host.channels.live.clear()
        resp = self._restart(host)
        assert resp.status_code == 200, resp.text
        assert resp.json()["restarted"] is False
        assert [e for e in host.events if e[0] == "stop"] == []
        assert len(host.channels.spawned) == 1

    def test_a_restart_clears_the_restart_due_hint(self, host):
        life_os = Path(host.channels.app.state.webapp_config.life_os_dir)
        skill_md = life_os / ".claude" / "skills" / "journal-daily" / "SKILL.md"
        now = time.time()
        _touch(skill_md, now - 10)
        host.channels.live[0]["started_at"] = now - 100

        def due():
            rows = host.channels.client.get("/api/life-os/channels").json()["profiles"]
            return next(p for p in rows if p["id"] == "health")["restart_due"]

        assert due() is True
        assert self._restart(host).status_code == 200
        assert due() is False

    def test_unknown_profile_is_a_404(self, host):
        resp = host.channels.client.post(
            "/api/life-os/channels/nope/restart", json={}
        )
        assert resp.status_code == 404
        assert host.events == []

    def test_a_launch_during_a_restart_waits_then_gets_409(self, host):
        """The Launch arrives while the quit is in flight: it waits on the
        profile's lock, then sees the fresh session and is refused."""
        import asyncio
        import threading
        import time as _time

        import httpx

        waited = threading.Event()
        from app.webapp.routers import life_os_channels
        fake_stop = life_os_channels.session_client.stop.side_effect

        def slow_stop(port, sid, mode="quit"):
            # Hold the quit until the Launch is queued on the lock.
            deadline = _time.monotonic() + 5
            while _time.monotonic() < deadline:
                lock = host.lock()
                if lock is not None and getattr(lock, "_waiters", None):
                    waited.set()
                    break
                _time.sleep(0.01)
            return fake_stop(port, sid, mode)

        life_os_channels.session_client.stop.side_effect = slow_stop

        async def go():
            transport = httpx.ASGITransport(app=host.channels.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as c:
                restart = asyncio.create_task(
                    c.post("/api/life-os/channels/health/restart", json={})
                )
                # The Restart holds the lock once its quit is in flight.
                while not any(e[0] == "mirror-close" for e in host.events):
                    await asyncio.sleep(0.01)
                launch = asyncio.create_task(
                    c.post("/api/life-os/channels/health/launch", json={})
                )
                return await restart, await launch

        restart, launch = asyncio.run(go())
        assert waited.is_set(), "the Launch never queued on the profile's lock"
        assert restart.status_code == 200, restart.text
        assert launch.status_code == 409, launch.text
        assert len(host.channels.spawned) == 1


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    os.utime(path, (mtime, mtime))


class TestRestartDue:
    """``restart_due`` (#1675): the skill or project settings changed after
    the running bot's session started. Only the yes/no answer leaves."""

    START = 1_800_000_000.0

    @pytest.fixture
    def due(self, channels):
        life_os = Path(channels.app.state.webapp_config.life_os_dir)
        skill_md = life_os / ".claude" / "skills" / "journal-daily" / "SKILL.md"
        _touch(skill_md, self.START - 100)
        channels.live.append({"session_id": "s-health", "label": "telegram:health",
                              "name": "journal-daily", "alive": True,
                              "started_at": self.START})

        def rows():
            body = channels.client.get("/api/life-os/channels").json()
            return {p["id"]: p for p in body["profiles"]}

        return SimpleNamespace(channels=channels, life_os=life_os,
                               skill_md=skill_md, rows=rows)

    def test_nothing_changed_since_start_is_false(self, due):
        assert due.rows()["health"]["restart_due"] is False

    def test_a_newer_skill_md_is_true(self, due):
        _touch(due.skill_md, self.START + 5)
        assert due.rows()["health"]["restart_due"] is True

    @pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
    def test_newer_project_settings_are_true(self, due, name):
        _touch(due.life_os / ".claude" / name, self.START + 5)
        assert due.rows()["health"]["restart_due"] is True

    def test_older_project_settings_are_false(self, due):
        _touch(due.life_os / ".claude" / "settings.json", self.START - 5)
        _touch(due.life_os / ".claude" / "settings.local.json", self.START - 5)
        assert due.rows()["health"]["restart_due"] is False

    def test_a_stopped_bot_is_unknown(self, due):
        assert due.rows()["school"]["restart_due"] is None

    def test_no_start_time_is_unknown(self, due):
        due.channels.live[0].pop("started_at")
        _touch(due.skill_md, self.START + 5)
        assert due.rows()["health"]["restart_due"] is None

    def test_host_down_is_unknown(self, due):
        from app.webapp.routers import life_os_channels
        err = life_os_channels.session_client.SessionHostError
        life_os_channels.session_client.list_sessions.side_effect = err("down")
        assert all(p["restart_due"] is None for p in due.rows().values())

    def test_an_unreadable_settings_file_is_unknown(self, due, monkeypatch):
        settings = due.life_os / ".claude" / "settings.json"
        _touch(settings, self.START - 5)
        real_stat = Path.stat

        def stat(self, *args, **kwargs):
            if self == settings:
                raise PermissionError("denied")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
        assert due.rows()["health"]["restart_due"] is None

    def test_no_path_leaves_the_payload(self, due):
        _touch(due.skill_md, self.START + 5)
        body = json.dumps(due.channels.client.get("/api/life-os/channels").json())
        assert "SKILL.md" not in body and "settings" not in body


# ------------------------------------------------------------- setup checks
class TestSetupChecks:
    """The Settings card's read-only checks (#1369)."""

    def test_plugin_install_record_three_states(self, tmp_path):
        life = tmp_path / "life-os"
        life.mkdir()
        record = tmp_path / "installed_plugins.json"

        def write(entries):
            record.write_text(json.dumps(
                {"plugins": {channel_profiles.PLUGIN_ID: entries}}
            ), encoding="utf-8")

        check = channel_profiles.plugin_installed_for
        assert check(life, tmp_path / "missing.json") is None  # unknown, not False
        record.write_text("{nope", encoding="utf-8")
        assert check(life, record) is None
        write([])
        assert check(life, record) is False
        write([{"scope": "project", "projectPath": str(tmp_path / "other")}])
        assert check(life, record) is False  # installed, but not for life-os
        write([{"scope": "project", "projectPath": str(life)}])
        assert check(life, record) is True
        write([{"scope": "user"}])
        assert check(life, record) is True

    def test_env_presence_is_a_stat_never_an_open(self, tmp_path, monkeypatch):
        profile = channel_profiles.ChannelProfile(
            "health", "Health", "health", _state_dir(tmp_path, "h")
        )
        assert channel_profiles.env_file_present(profile) is False
        (tmp_path / "h" / ".env").write_text("TELEGRAM_BOT_TOKEN=secret", "utf-8")
        opened: list = []
        real_open = Path.open
        monkeypatch.setattr(
            Path, "open", lambda self, *a, **k: opened.append(self) or real_open(self, *a, **k)
        )
        monkeypatch.setattr(
            Path, "read_text",
            lambda self, *a, **k: pytest.fail(f"read {self}"),
        )
        assert channel_profiles.env_file_present(profile) is True
        assert opened == []

    def test_setup_block_reports_checks_and_leaks_nothing(
        self, life_os_client, tmp_path, monkeypatch
    ):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware, "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )
        client, app, _ = life_os_client
        from app.webapp.routers import life_os_channels
        life_os_channels.session_client.list_sessions.side_effect = lambda port: []
        state = _state_dir(tmp_path, "tg-secret-dir")
        (tmp_path / "tg-secret-dir" / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=123456:TOP-SECRET-TOKEN", encoding="utf-8"
        )
        app.state.channel_profiles_path = _write_profiles(tmp_path / "p.json", [
            {"id": "health", "skill": "journal-daily", "state_dir": state},
            {"id": "school", "skill": "no-such-skill",
             "state_dir": _state_dir(tmp_path, "tg-other")},
        ])
        app.state.installed_plugins_path = tmp_path / "absent.json"
        body = client.get("/api/life-os/channels").json()
        rows = {p["id"]: p for p in body["profiles"]}
        assert rows["health"]["env_present"] is True
        assert rows["school"]["env_present"] is False
        assert rows["school"]["skill_found"] is False
        setup = body["setup"]
        assert setup["file_present"] is True
        assert setup["life_os_found"] is True
        assert setup["plugin"] is None  # unreadable record → unknown
        assert isinstance(setup["bun"], bool)
        assert any(s["id"] == "journal-daily" for s in setup["skills"])
        raw = json.dumps(body)
        assert "tg-secret-dir" not in raw and "tg-other" not in raw
        assert "TOP-SECRET" not in raw and str(tmp_path) not in raw

    def test_setup_without_a_profile_file(
        self, life_os_client, tmp_path, monkeypatch
    ):
        from app.webapp import middleware
        monkeypatch.setattr(
            middleware, "LOOPBACK_HOSTS",
            frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
        )
        client, app, _ = life_os_client
        app.state.channel_profiles_path = tmp_path / "nope.json"
        body = client.get("/api/life-os/channels").json()
        assert body["profiles"] == [] and body["problems"] == []
        assert body["setup"]["file_present"] is False
