"""POST /api/config: allow-list and validation."""

from __future__ import annotations

import json

import pytest

from src.webapp_config import update_webapp_config


class TestPatchConfig:
    def test_patches_allowed_field(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_effort": "low"}
        )
        assert resp.status_code == 200
        assert "--effort low" in resp.json()["claude_flags"]
        # And the in-memory cfg was swapped: the override is for the selected
        # model only (#1711), not a global value.
        assert app.state.webapp_config.claude_model_efforts == {"opus": "low"}

    def test_claude_effort_post_changes_the_selected_model_only(self, webapp_client):
        client, app, _ = webapp_client
        assert client.post("/api/config", json={"claude_effort": "max"}).status_code == 200
        sonnet = client.post("/api/config", json={"claude_model": "sonnet"})
        assert sonnet.status_code == 200
        # Sonnet never inherits Opus's override: it runs at its own default.
        assert "--effort high" in sonnet.json()["claude_flags"]
        claude = client.get("/api/config").json()["claude"]
        assert claude["effort"] == "high" and claude["default_effort"] == "high"
        assert claude["model_efforts"] == {"opus": "max"}
        assert app.state.webapp_config.claude_model_efforts == {"opus": "max"}

    def test_claude_effort_empty_string_clears_the_override(self, webapp_client):
        client, app, _ = webapp_client
        client.post("/api/config", json={"claude_effort": "low"})
        cleared = client.post("/api/config", json={"claude_effort": ""})
        assert cleared.status_code == 200
        assert app.state.webapp_config.claude_model_efforts == {}
        assert "--effort medium" in cleared.json()["claude_flags"]

    def test_claude_effort_applies_to_a_co_patched_model(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_model": "fable", "claude_effort": "xhigh"}
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.claude_model_efforts == {"fable": "xhigh"}

    def test_claude_effort_override_survives_a_reload_from_disk(self, webapp_client):
        from src.webapp_config import load_webapp_config

        client, _, overrides = webapp_client
        client.post("/api/config", json={"claude_effort": "off"})
        on_disk = json.loads(overrides["tmp_webapp_cfg_path"].read_text())
        assert on_disk["claude_model_efforts"] == {"opus": "off"}
        assert "claude_effort" not in on_disk
        reloaded = load_webapp_config(overrides["tmp_webapp_cfg_path"])
        assert reloaded.claude_model_efforts == {"opus": "off"}

    def test_claude_effort_unsupported_value_is_a_400(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"claude_effort": "ultra"})
        assert resp.status_code == 400
        assert app.state.webapp_config.claude_model_efforts == {}

    def test_get_config_reports_the_effective_effort_per_model(self, webapp_client):
        client, _, _ = webapp_client
        claude = client.get("/api/config").json()["claude"]
        assert (claude["model"], claude["effort"], claude["default_effort"]) == (
            "opus", "medium", "medium",
        )
        assert claude["model_efforts"] == {}
        assert "--effort medium" in claude["computed_flags"]

    def test_patches_claude_model_to_fable(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"claude_model": "fable"})
        assert resp.status_code == 200
        assert "--model fable" in resp.json()["claude_flags"]
        assert app.state.webapp_config.claude_model == "fable"

    def test_patch_never_persists_session_host_port_env_override(
        self, webapp_client, monkeypatch
    ):
        """The e2e pre-ship gate's disposable webapp sets
        LAUNCHER_SESSION_HOST_PORT to point at a disposable session-host
        (issue #260) rather than the live :8446. A config PATCH against
        that disposable webapp must never bake the override into the
        real, shared config/webapp_config.json on save — only load it
        for the running process's own in-memory use. Discovered live: a
        Settings-tab e2e test triggered exactly this, overwriting the
        user's real session_host_port with a disposable autoboot port
        and taking the live session-host down until the file was
        hand-repaired (issue #435 follow-up)."""
        client, app, overrides = webapp_client
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert json.loads(cfg_path.read_text())["session_host_port"] == 8446

        monkeypatch.setenv("LAUNCHER_SESSION_HOST_PORT", "59999")
        resp = client.post("/api/config", json={"claude_effort": "low"})
        assert resp.status_code == 200

        # The running process's own state sees the override — it must keep
        # talking to the disposable session-host for the rest of its life.
        assert app.state.webapp_config.session_host_port == 59999
        # But the shared on-disk file must be untouched.
        on_disk = json.loads(cfg_path.read_text())
        assert on_disk["session_host_port"] == 8446

    def test_rejects_invalid_value_with_400(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_model": "definitely-not-a-real-model"}
        )
        assert resp.status_code == 400

    def test_permission_mode_round_trips(self, webapp_client):
        """claude_permission_mode patches through: 'skip' swaps the default
        --permission-mode auto for the legacy --dangerously-skip-permissions,
        and the choice surfaces on the next GET."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_permission_mode": "skip"}
        )
        assert resp.status_code == 200
        flags = resp.json()["claude_flags"]
        assert "--dangerously-skip-permissions" in flags
        assert "--permission-mode auto" not in flags
        assert app.state.webapp_config.claude_permission_mode == "skip"
        claude = client.get("/api/config").json()["claude"]
        assert claude["permission_mode"] == "skip"

    def test_rejects_invalid_permission_mode_with_400(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_permission_mode": "bogus"}
        )
        assert resp.status_code == 400

    def test_projects_ignore_round_trips(self, webapp_client):
        """projects_ignore is a list field — the endpoint accepts it,
        strips blank entries, and persists it on the in-memory cfg."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config",
            json={"projects_ignore": ["archive", "  ", "*-old"]},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.projects_ignore == ["archive", "*-old"]
        # And it survives a GET round-trip.
        body = client.get("/api/config").json()
        assert body["projects_ignore"] == ["archive", "*-old"]

    def test_coding_hidden_agents_round_trips_and_persists(self, webapp_client):
        """coding_hidden_agents (issue #666) is a list field like
        projects_ignore: it patches through, strips blanks, surfaces on the
        next GET, and actually reaches disk — a hidden button must stay
        hidden across a webapp restart, not just in process state."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        # Default is empty — every button visible until the user hides one.
        assert client.get("/api/config").json()["coding_hidden_agents"] == []
        resp = client.post(
            "/api/config",
            json={"coding_hidden_agents": ["antigravity", "  ", "github"]},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.coding_hidden_agents == [
            "antigravity",
            "github",
        ]
        body = client.get("/api/config").json()
        assert body["coding_hidden_agents"] == ["antigravity", "github"]
        cfg_path = overrides["tmp_webapp_cfg_path"]
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["coding_hidden_agents"] == ["antigravity", "github"]
        assert load_webapp_config(cfg_path).coding_hidden_agents == [
            "antigravity",
            "github",
        ]
        # Un-hiding clears it back out.
        client.post("/api/config", json={"coding_hidden_agents": []})
        assert client.get("/api/config").json()["coding_hidden_agents"] == []

    def test_coding_favorite_agent_round_trips_and_rejects_unknown_ids(
        self, webapp_client
    ):
        """coding_favorite_agent (#1070) names the one agent whose launch
        button stays on a Coding project row.

        Unlike the list fields beside it this one is *validated*, not just
        coerced to a clean string: the row renders exactly one launch button
        from it, so an id that names no registered agent would leave the row
        with no way to launch anything. An unknown id — a typo, a stale
        client, an agent dropped from the registry — falls back to
        DEFAULT_AGENT rather than being stored.
        """
        from src.agents import DEFAULT_AGENT
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        # Defaults to the registry's own default agent, so a fresh config
        # already renders a working row.
        assert client.get("/api/config").json()["coding_favorite_agent"] == DEFAULT_AGENT

        resp = client.post("/api/config", json={"coding_favorite_agent": "codex"})
        assert resp.status_code == 200
        assert app.state.webapp_config.coding_favorite_agent == "codex"
        assert client.get("/api/config").json()["coding_favorite_agent"] == "codex"
        # Reaches disk — the favourite must survive a webapp restart.
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).coding_favorite_agent == "codex"

        # An unknown id is refused into the default, never stored.
        for junk in ("not-an-agent", "", "   ", None):
            resp = client.post("/api/config", json={"coding_favorite_agent": junk})
            assert resp.status_code == 200, junk
            assert app.state.webapp_config.coding_favorite_agent == DEFAULT_AGENT, junk

    def test_hide_channel_sessions_defaults_on_and_round_trips(self, webapp_client):
        """hide_channel_sessions (#1384): Telegram sessions leave the Board and
        the session list unless the user turns it off. Default ON; the choice
        must reach disk (a switch that reverts on restart would put the
        household's Stop button back); only a real boolean is accepted."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["hide_channel_sessions"] is True

        resp = client.post("/api/config", json={"hide_channel_sessions": False})
        assert resp.status_code == 200
        assert app.state.webapp_config.hide_channel_sessions is False
        assert client.get("/api/config").json()["hide_channel_sessions"] is False
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).hide_channel_sessions is False

        resp = client.post("/api/config", json={"hide_channel_sessions": True})
        assert resp.status_code == 200
        assert load_webapp_config(cfg_path).hide_channel_sessions is True

    @pytest.mark.parametrize("junk", ["false", "no", 0, 1, None, []])
    def test_hide_channel_sessions_rejects_non_booleans(self, webapp_client, junk):
        """"false" is truthy in Python: a string must never flip the switch."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"hide_channel_sessions": junk})
        assert resp.status_code == 400
        assert app.state.webapp_config.hide_channel_sessions is True

    def test_autostart_channel_sessions_defaults_off_and_round_trips(
        self, webapp_client
    ):
        """autostart_channel_sessions (#1542): starts the Telegram bots when the
        webapp starts. Default OFF (it launches real sessions); the choice must
        reach disk, because the webapp only reads it when it next starts."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["autostart_channel_sessions"] is False

        resp = client.post("/api/config", json={"autostart_channel_sessions": True})
        assert resp.status_code == 200
        assert app.state.webapp_config.autostart_channel_sessions is True
        assert client.get("/api/config").json()["autostart_channel_sessions"] is True
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).autostart_channel_sessions is True

        resp = client.post("/api/config", json={"autostart_channel_sessions": False})
        assert resp.status_code == 200
        assert load_webapp_config(cfg_path).autostart_channel_sessions is False

    @pytest.mark.parametrize("junk", ["true", "yes", 0, 1, None, []])
    def test_autostart_channel_sessions_rejects_non_booleans(
        self, webapp_client, junk
    ):
        """"true" is a string: only a real boolean may arm a launch at boot."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"autostart_channel_sessions": junk})
        assert resp.status_code == 400
        assert app.state.webapp_config.autostart_channel_sessions is False

    def test_chief_doorbell_defaults_off_and_round_trips(self, webapp_client):
        """chief_doorbell (#1645): types into the live chief, so default OFF.
        The save swaps app.state.webapp_config, which the tick re-reads every
        cycle, and reaches disk so it survives a restart."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["chief_doorbell"] is False

        resp = client.post("/api/config", json={"chief_doorbell": True})
        assert resp.status_code == 200
        assert app.state.webapp_config.chief_doorbell is True
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).chief_doorbell is True

        resp = client.post("/api/config", json={"chief_doorbell": False})
        assert resp.status_code == 200
        assert load_webapp_config(cfg_path).chief_doorbell is False

    def test_chief_job_failures_defaults_on_and_round_trips(self, webapp_client):
        """chief_job_failures (#1783): the swap on app.state is what the next
        failed run reads, and the save reaches disk."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["chief_job_failures"] is True

        resp = client.post("/api/config", json={"chief_job_failures": False})
        assert resp.status_code == 200
        assert app.state.webapp_config.chief_job_failures is False
        cfg_path = overrides["tmp_webapp_cfg_path"]
        assert load_webapp_config(cfg_path).chief_job_failures is False
        assert client.get("/api/config").json()["chief_job_failures"] is False

    def test_grok_auto_switch_defaults_off_and_round_trips(self, webapp_client):
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        assert client.get("/api/config").json()["grok_auto_switch"] is False
        assert client.post("/api/config", json={"grok_auto_switch": True}).status_code == 200
        assert load_webapp_config(overrides["tmp_webapp_cfg_path"]).grok_auto_switch is True
        assert client.post("/api/config", json={"grok_auto_switch": "true"}).status_code == 400
        assert app.state.webapp_config.grok_auto_switch is True

    @pytest.mark.parametrize("junk", ["false", 0, None])
    def test_chief_job_failures_rejects_non_booleans(self, webapp_client, junk):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_job_failures": junk})
        assert resp.status_code == 400
        assert app.state.webapp_config.chief_job_failures is True

    @pytest.mark.parametrize("junk", ["true", "yes", 0, 1, None, []])
    def test_chief_doorbell_rejects_non_booleans(self, webapp_client, junk):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"chief_doorbell": junk})
        assert resp.status_code == 400
        assert app.state.webapp_config.chief_doorbell is False

    def test_usage_providers_default_and_round_trip(self, webapp_client):
        """usage_providers: which providers the usage meter draws, each on or
        off by itself. Default Claude and Codex; every set (the empty one
        hides the card) must reach disk so it applies on every device, stored
        in card order whatever order the client sent."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        body = client.get("/api/config").json()
        assert body["usage_providers"] == ["claude", "codex"]
        assert body["usage_providers_available"] == ["claude", "codex", "grok"]

        cfg_path = overrides["tmp_webapp_cfg_path"]
        for sent, stored in (
            (["grok"], ["grok"]),
            (["grok", "claude"], ["claude", "grok"]),
            ([], []),
            (["codex", "grok", "claude"], ["claude", "codex", "grok"]),
        ):
            resp = client.post("/api/config", json={"usage_providers": sent})
            assert resp.status_code == 200, sent
            assert app.state.webapp_config.usage_providers == stored
            assert client.get("/api/config").json()["usage_providers"] == stored
            assert load_webapp_config(cfg_path).usage_providers == stored

    @pytest.mark.parametrize("junk", ["claude", ["Claude"], ["all"], [1], None, True])
    def test_usage_providers_rejects_unknown_values(self, webapp_client, junk):
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"usage_providers": junk})
        assert resp.status_code == 400
        assert app.state.webapp_config.usage_providers == ["claude", "codex"]

    def test_config_reports_chief_model_and_worker_cap_read_only(self, webapp_client):
        """Settings › Chief's row (#1435) reads the chief's model and worker
        cap from GET /api/config, so showing them needs no passkey. Writes
        stay on PUT /api/board/chief/settings: POST /api/config ignores them."""
        client, app, _ = webapp_client
        body = client.get("/api/config").json()
        assert body["chief_model"] == app.state.webapp_config.chief_model
        assert body["chief_worker_cap"] == app.state.webapp_config.chief_worker_cap
        assert (body["chief_worker_cap_min"], body["chief_worker_cap_max"]) == (1, 10)

        # What a PUT /api/board/chief/settings stores (that route is
        # passkey-gated, covered in tests/api/chief/).
        app.state.webapp_config = update_webapp_config(chief_model="opus", chief_worker_cap=5)
        body = client.get("/api/config").json()
        assert (body["chief_model"], body["chief_worker_cap"]) == ("opus", 5)

        client.post("/api/config", json={"chief_model": "sonnet", "chief_worker_cap": 2})
        assert (app.state.webapp_config.chief_model, app.state.webapp_config.chief_worker_cap) == ("opus", 5)

    def test_terminal_history_lines_round_trips(self, webapp_client):
        """terminal_history_lines (issue #435 follow-up, Settings tab) is
        in the allow-list — it patches through, surfaces on the next GET,
        AND actually reaches disk (issue #441: it originally shipped
        missing from both load_webapp_config's constructor call and
        save_webapp_config's payload, so a Save was in-memory only and
        silently reverted on restart or on the next unrelated PATCH —
        which the old in-memory-only assertions here couldn't catch)."""
        from src.webapp_config import load_webapp_config

        client, app, overrides = webapp_client
        resp = client.post("/api/config", json={"terminal_history_lines": 5000})
        assert resp.status_code == 200
        assert app.state.webapp_config.terminal_history_lines == 5000
        body = client.get("/api/config").json()
        assert body["terminal_history_lines"] == 5000
        # Persisted, not just process state: the on-disk JSON carries it and
        # a fresh load (a webapp restart, or update_webapp_config's
        # reload-before-save on any later PATCH) round-trips it.
        cfg_path = overrides["tmp_webapp_cfg_path"]
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["terminal_history_lines"] == 5000
        assert load_webapp_config(cfg_path).terminal_history_lines == 5000

    def test_terminal_history_lines_rejects_out_of_range(self, webapp_client):
        client, _, _ = webapp_client
        too_low = client.post("/api/config", json={"terminal_history_lines": 1})
        assert too_low.status_code == 400
        too_high = client.post(
            "/api/config", json={"terminal_history_lines": 1_000_000}
        )
        assert too_high.status_code == 400

    def test_fleet_config_dir_round_trips(self, webapp_client):
        """fleet_config_dir is in the allow-list —
        it patches through and surfaces on the next GET."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"fleet_config_dir": "E:\\automation\\fleet-config"}
        )
        assert resp.status_code == 200
        assert (
            app.state.webapp_config.fleet_config_dir
            == "E:\\automation\\fleet-config"
        )
        body = client.get("/api/config").json()
        assert body["fleet_config_dir"] == "E:\\automation\\fleet-config"

    def test_antigravity_toggles_round_trip(self, webapp_client):
        """The two Antigravity launch toggles patch through and surface
        as composed `agy` flags on the next GET."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config",
            json={"antigravity_skip_permissions": True, "antigravity_sandbox": True},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.antigravity_skip_permissions is True
        assert app.state.webapp_config.antigravity_sandbox is True
        ag = client.get("/api/config").json()["antigravity"]
        assert ag["skip_permissions"] is True
        assert ag["sandbox"] is True
        assert "--dangerously-skip-permissions" in ag["computed_flags"]
        assert "--sandbox" in ag["computed_flags"]

    def test_grok_knobs_round_trip(self, webapp_client):
        """Grok reasoning tier + permission mode patch through and surface as
        composed `grok` flags (#667). 'skip' swaps `auto` for
        `bypassPermissions`; an invalid tier is rejected with 400 rather
        than silently launching a CLI that would reject it anyway."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config",
            json={"grok_effort": "low", "grok_permission_mode": "skip"},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.grok_effort == "low"
        assert app.state.webapp_config.grok_permission_mode == "skip"
        gk = client.get("/api/config").json()["grok"]
        assert gk["effort"] == "low"
        assert gk["permission_mode"] == "skip"
        assert "--permission-mode bypassPermissions" in gk["computed_flags"]
        assert "--reasoning-effort low" in gk["computed_flags"]
        # No model flag while `grok models` lists a single entry (#667).
        assert "--model" not in gk["computed_flags"]
        bad = client.post("/api/config", json={"grok_effort": "ultra"})
        assert bad.status_code == 400

    def test_codex_knobs_round_trip(self, webapp_client):
        """Codex reasoning tier + permission mode patch through and surface
        as composed `codex` flags. 'skip' swaps the sandboxed auto pair for
        the all-bypass switch; an invalid tier is rejected with 400."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config",
            json={"codex_effort": "low", "codex_permission_mode": "skip"},
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.codex_effort == "low"
        assert app.state.webapp_config.codex_permission_mode == "skip"
        cx = client.get("/api/config").json()["codex"]
        assert cx["effort"] == "low"
        assert cx["permission_mode"] == "skip"
        assert "--dangerously-bypass-approvals-and-sandbox" in cx["computed_flags"]
        assert "--ask-for-approval" not in cx["computed_flags"]
        assert "model_reasoning_effort=low" in cx["computed_flags"]
        # An unknown reasoning tier is rejected, not silently launched.
        bad = client.post("/api/config", json={"codex_effort": "ultra"})
        assert bad.status_code == 400

    def test_codex_model_remembers_compatible_effort(self, webapp_client):
        client, app, _ = webapp_client
        # Terra, Sol and Astra are hidden by default; a hidden default falls back.
        client.post("/api/config", json={"hidden_models": []})
        assert client.post(
            "/api/config",
            json={"codex_model": "gpt-5.6-terra", "codex_effort": "ultra"},
        ).status_code == 200
        assert client.post(
            "/api/config", json={"codex_model": "gpt-6-luna"}
        ).status_code == 200
        assert app.state.webapp_config.codex_effort == "xhigh"
        assert client.post(
            "/api/config", json={"codex_model": "gpt-5.6-terra"}
        ).status_code == 200
        assert app.state.webapp_config.codex_effort == "ultra"
        assert "--model gpt-5.6-terra" in (
            client.get("/api/config").json()["codex"]["computed_flags"]
        )

    def test_astra_model_and_effort_can_be_persisted(self, webapp_client):
        client, app, _ = webapp_client
        # Terra, Sol and Astra are hidden by default; a hidden default falls back.
        client.post("/api/config", json={"hidden_models": []})
        resp = client.post(
            "/api/config",
            json={"codex_model": "gpt-6-astra", "codex_effort": "max"},
        )
        assert resp.status_code == 200, resp.text
        assert app.state.webapp_config.codex_model == "gpt-6-astra"
        assert app.state.webapp_config.codex_effort == "max"
        flags = client.get("/api/config").json()["codex"]["computed_flags"]
        assert "--model gpt-6-astra" in flags
        assert "model_reasoning_effort=max" in flags

    def test_copilot_toggle_round_trips(self, webapp_client):
        """The Copilot launch toggle patches through and surfaces as the
        composed `copilot` flag on the next GET."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"copilot_skip_permissions": True}
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.copilot_skip_permissions is True
        cp = client.get("/api/config").json()["copilot"]
        assert cp["skip_permissions"] is True
        assert "--allow-all" in cp["computed_flags"]

    def test_copilot_launch_never_sends_a_model_flag(self, webapp_client):
        """Copilot is launched with no `--model`, and the API offers no
        model setting to send one with (issue #1017).

        The launcher cannot know before a launch whether Copilot will
        honour a model id — entitlement is resolved per launch against
        the account, and a refusal only prints one line that scrolls
        away — so displaying a chosen model was asserting a setting the
        session might not be running. The honest shape is to send
        nothing and let `/model` own it, which is what this pins: no
        model in the payload, no catalogue to drift, no `--model` in the
        composed flags however the toggle is set, and the removed key
        silently ignored rather than quietly persisted.
        """
        client, app, _ = webapp_client
        cp = client.get("/api/config").json()["copilot"]
        assert "model" not in cp
        assert "models_available" not in cp
        assert "--model" not in cp["computed_flags"]

        # The allow-all toggle is the only Copilot launch switch, and
        # turning it on still brings no model flag with it.
        assert client.post(
            "/api/config", json={"copilot_skip_permissions": True}
        ).status_code == 200
        cp = client.get("/api/config").json()["copilot"]
        assert cp["computed_flags"] == "--allow-all"

        # The retired key is not in the allow-list: it neither 400s nor
        # lands on the config, so a stale client cannot resurrect it.
        resp = client.post("/api/config", json={"copilot_model": "gpt-6-luna"})
        assert resp.status_code == 200
        assert not hasattr(app.state.webapp_config, "copilot_model")
        assert "--model" not in client.get("/api/config").json()["copilot"][
            "computed_flags"
        ]

    def test_pi_model_round_trips(self, webapp_client):
        """A valid Pi model patches through and surfaces in the forced
        explicit-provider launch flags; an invalid one is rejected with 400."""
        client, app, _ = webapp_client
        # Sonnet stays on the claude-agent-sdk subscription path.
        resp = client.post("/api/config", json={"pi_model": "claude-sonnet-5"})
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_model == "claude-sonnet-5"
        pi = client.get("/api/config").json()["pi"]
        assert pi["model"] == "claude-sonnet-5"
        assert (
            "--provider claude-agent-sdk "
            "--model claude-agent-sdk/claude-sonnet-5" in pi["computed_flags"]
        )
        # An unknown model is rejected, not silently launched onto a bad provider.
        bad = client.post("/api/config", json={"pi_model": "claude-not-real"})
        assert bad.status_code == 400

    def test_pi_gpt_switches_provider(self, webapp_client):
        """Selecting GPT routes pi to the openai-codex subscription provider —
        the one cross-provider option — not claude-agent-sdk."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"pi_model": "gpt-5.6-sol"})
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_model == "gpt-5.6-sol"
        flags = client.get("/api/config").json()["pi"]["computed_flags"]
        assert "--provider openai-codex --model openai-codex/gpt-5.6-sol" in flags
        assert "claude-agent-sdk" not in flags

    def test_pi_effort_round_trips(self, webapp_client):
        """A pi_effort POST is the override for the selected Pi model (#1713),
        surfaces as --thinking, and an invalid tier is rejected with 400."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"pi_effort": "low"})
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_model_efforts == {"claude-opus-5": "low"}
        pi = client.get("/api/config").json()["pi"]
        assert pi["effort"] == "low" and pi["default_effort"] == "high"
        assert pi["model_efforts"] == {"claude-opus-5": "low"}
        assert "--thinking low" in pi["computed_flags"]
        bad = client.post("/api/config", json={"pi_effort": "ultra"})
        assert bad.status_code == 400
        assert app.state.webapp_config.pi_model_efforts == {"claude-opus-5": "low"}

    def test_pi_effort_post_changes_the_selected_model_only(self, webapp_client):
        client, app, _ = webapp_client
        assert client.post("/api/config", json={"pi_effort": "medium"}).status_code == 200
        assert client.post("/api/config", json={"pi_model": "gpt-5.6-sol"}).status_code == 200
        # Sol never inherits Opus's override: it runs at its own default.
        pi = client.get("/api/config").json()["pi"]
        assert pi["effort"] == "high" and "--thinking high" in pi["computed_flags"]
        assert pi["model_efforts"] == {"claude-opus-5": "medium"}

    def test_pi_effort_empty_string_clears_the_override(self, webapp_client):
        client, app, _ = webapp_client
        client.post("/api/config", json={"pi_effort": "low"})
        assert client.post("/api/config", json={"pi_effort": ""}).status_code == 200
        assert app.state.webapp_config.pi_model_efforts == {}
        pi = client.get("/api/config").json()["pi"]
        assert pi["effort"] == "high" and "--thinking high" in pi["computed_flags"]

    def test_pi_effort_applies_to_a_co_patched_model(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"pi_model": "gpt-5.6-terra", "pi_effort": "xhigh"}
        )
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_model_efforts == {"gpt-5.6-terra": "xhigh"}

    def test_pi_effort_override_survives_a_reload_from_disk(self, webapp_client):
        from src.webapp_config import load_webapp_config

        client, _, overrides = webapp_client
        client.post("/api/config", json={"pi_effort": "off"})
        on_disk = json.loads(overrides["tmp_webapp_cfg_path"].read_text())
        assert on_disk["pi_model_efforts"] == {"claude-opus-5": "off"}
        assert "pi_effort" not in on_disk
        reloaded = load_webapp_config(overrides["tmp_webapp_cfg_path"])
        assert reloaded.pi_model_efforts == {"claude-opus-5": "off"}

    def test_pi_trust_mode_round_trips(self, webapp_client):
        """pi_trust_mode patches through: 'ask' swaps --approve for
        --no-approve; an invalid value is rejected with 400."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"pi_trust_mode": "ask"})
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_trust_mode == "ask"
        pi = client.get("/api/config").json()["pi"]
        assert pi["trust_mode"] == "ask"
        assert "--no-approve" in pi["computed_flags"]
        assert "--approve" not in pi["computed_flags"].replace("--no-approve", "")
        bad = client.post("/api/config", json={"pi_trust_mode": "bogus"})
        assert bad.status_code == 400

    def test_ignores_unknown_field_silently(self, webapp_client):
        """The endpoint filters by allow-list — unknown keys are dropped,
        not error'd. Confirms the whitelist isn't accidentally loosened."""
        client, app, _ = webapp_client
        before = app.state.webapp_config.claude_model
        resp = client.post(
            "/api/config",
            json={"auth_token": "should-be-ignored", "claude_model": before},
        )
        assert resp.status_code == 200
        # auth_token is NOT in the allow-list and must not be patched here.
        assert app.state.webapp_config.auth_token == ""
