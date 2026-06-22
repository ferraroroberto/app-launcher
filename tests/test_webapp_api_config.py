"""GET / POST /api/config — shape + allow-list + validation."""

from __future__ import annotations

import pytest


class TestGetConfig:
    def test_returns_expected_shape(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "host" in body
        assert "port" in body
        assert "projects_dir" in body
        assert "projects_ignore" in body
        assert isinstance(body["projects_ignore"], list)
        assert "apps_scan_root" in body
        assert "life_os_dir" in body
        assert "claude_config_dir" in body
        assert "claude" in body
        # auth_password_set is what the SPA shows in the login overlay
        # ("a password is required" vs not). Bool, not the password itself.
        assert isinstance(body["auth_password_set"], bool)
        assert body["auth_password_set"] is False  # default conftest config

    def test_claude_block_carries_all_known_keys(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        claude = body["claude"]
        for key in (
            "model",
            "effort",
            "verbose",
            "debug",
            "permission_mode",
            "models_available",
            "efforts_available",
            "permission_modes_available",
            "always_on_flags",
            "computed_flags",
        ):
            assert key in claude, f"missing key {key} in /api/config claude block"

    def test_antigravity_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        ag = body["antigravity"]
        assert set(ag) == {"skip_permissions", "sandbox", "computed_flags"}
        assert isinstance(ag["skip_permissions"], bool)
        assert isinstance(ag["sandbox"], bool)
        # All-default config → the CLI is launched bare.
        assert ag["computed_flags"] == ""

    def test_codex_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        cx = body["codex"]
        assert set(cx) == {
            "effort",
            "permission_mode",
            "efforts_available",
            "permission_modes_available",
            "computed_flags",
        }
        assert isinstance(cx["efforts_available"], list) and cx["efforts_available"]
        # Default config → high reasoning + auto permission (sandboxed,
        # no prompts): the safe autopilot, not the all-bypass switch.
        assert cx["effort"] == "high"
        assert cx["permission_mode"] == "auto"
        assert "--ask-for-approval never" in cx["computed_flags"]
        assert "--sandbox workspace-write" in cx["computed_flags"]
        assert "model_reasoning_effort=high" in cx["computed_flags"]

    def test_copilot_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        cp = body["copilot"]
        assert set(cp) == {
            "skip_permissions", "model", "models_available", "computed_flags"
        }
        assert isinstance(cp["skip_permissions"], bool)
        assert isinstance(cp["models_available"], list) and cp["models_available"]
        # Default config → no model pinned, the CLI is launched bare.
        assert cp["model"] == ""
        assert cp["computed_flags"] == ""

    def test_pi_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        pi = body["pi"]
        assert set(pi) == {
            "model",
            "effort",
            "trust_mode",
            "models_available",
            "efforts_available",
            "trust_modes_available",
            "computed_flags",
        }
        # models_available carries {value,label} so the segmented buttons can
        # read "Opus/Sonnet/GPT" rather than the raw model ids.
        assert isinstance(pi["models_available"], list) and pi["models_available"]
        values = [m["value"] for m in pi["models_available"]]
        labels = [m["label"] for m in pi["models_available"]]
        assert pi["model"] in values
        assert labels == ["Opus", "Sonnet", "GPT"]
        assert pi["effort"] in pi["efforts_available"]
        assert pi["trust_mode"] in pi["trust_modes_available"]
        # Default config → Opus on the claude-agent-sdk subscription path,
        # high thinking, project trust on. Explicit provider/model always —
        # never bare, never the native billing provider.
        assert pi["computed_flags"] == (
            "--provider claude-agent-sdk --model claude-agent-sdk/claude-opus-4-8 "
            "--thinking high --approve"
        )


class TestPatchConfig:
    def test_patches_allowed_field(self, webapp_client):
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_effort": "low"}
        )
        assert resp.status_code == 200
        assert "--effort low" in resp.json()["claude_flags"]
        # And the in-memory cfg was swapped.
        assert app.state.webapp_config.claude_effort == "low"

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

    def test_claude_config_dir_round_trips(self, webapp_client):
        """claude_config_dir (system map, issue #173) is in the allow-list —
        it patches through and surfaces on the next GET."""
        client, app, _ = webapp_client
        resp = client.post(
            "/api/config", json={"claude_config_dir": "E:\\automation\\fleet-config"}
        )
        assert resp.status_code == 200
        assert (
            app.state.webapp_config.claude_config_dir
            == "E:\\automation\\fleet-config"
        )
        body = client.get("/api/config").json()
        assert body["claude_config_dir"] == "E:\\automation\\fleet-config"

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

    def test_copilot_model_round_trips(self, webapp_client):
        """A valid Copilot model patches through and surfaces as a
        `--model` flag; an invalid one is rejected with 400."""
        client, app, _ = webapp_client
        model = client.get("/api/config").json()["copilot"]["models_available"][0]
        resp = client.post("/api/config", json={"copilot_model": model})
        assert resp.status_code == 200
        assert app.state.webapp_config.copilot_model == model
        cp = client.get("/api/config").json()["copilot"]
        assert cp["model"] == model
        assert f"--model {model}" in cp["computed_flags"]
        # An unknown model is rejected, not silently launched.
        bad = client.post("/api/config", json={"copilot_model": "gpt-not-real"})
        assert bad.status_code == 400

    def test_pi_model_round_trips(self, webapp_client):
        """A valid Pi model patches through and surfaces in the forced
        explicit-provider launch flags; an invalid one is rejected with 400."""
        client, app, _ = webapp_client
        # Sonnet stays on the claude-agent-sdk subscription path.
        resp = client.post("/api/config", json={"pi_model": "claude-sonnet-4-6"})
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_model == "claude-sonnet-4-6"
        pi = client.get("/api/config").json()["pi"]
        assert pi["model"] == "claude-sonnet-4-6"
        assert (
            "--provider claude-agent-sdk "
            "--model claude-agent-sdk/claude-sonnet-4-6" in pi["computed_flags"]
        )
        # An unknown model is rejected, not silently launched onto a bad provider.
        bad = client.post("/api/config", json={"pi_model": "claude-not-real"})
        assert bad.status_code == 400

    def test_pi_gpt_switches_provider(self, webapp_client):
        """Selecting GPT routes pi to the openai-codex subscription provider —
        the one cross-provider option — not claude-agent-sdk."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"pi_model": "gpt-5.5"})
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_model == "gpt-5.5"
        flags = client.get("/api/config").json()["pi"]["computed_flags"]
        assert "--provider openai-codex --model openai-codex/gpt-5.5" in flags
        assert "claude-agent-sdk" not in flags

    def test_pi_effort_round_trips(self, webapp_client):
        """pi_effort patches through and surfaces as --thinking; an invalid
        tier is rejected with 400."""
        client, app, _ = webapp_client
        resp = client.post("/api/config", json={"pi_effort": "low"})
        assert resp.status_code == 200
        assert app.state.webapp_config.pi_effort == "low"
        pi = client.get("/api/config").json()["pi"]
        assert pi["effort"] == "low"
        assert "--thinking low" in pi["computed_flags"]
        bad = client.post("/api/config", json={"pi_effort": "ultra"})
        assert bad.status_code == 400

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
