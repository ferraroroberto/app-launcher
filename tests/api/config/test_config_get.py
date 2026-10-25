"""GET /api/config shape, the boot-autostart switch and the Chat send folders."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.boot_autostart as boot_autostart_mod


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
        assert "fleet_config_dir" in body
        assert "terminal_history_lines" in body
        assert isinstance(body["terminal_history_lines"], int)
        assert "terminal_history_lines_min" in body
        assert "terminal_history_lines_max" in body
        assert (
            body["terminal_history_lines_min"]
            <= body["terminal_history_lines"]
            <= body["terminal_history_lines_max"]
        )
        assert "claude" in body
        # auth_password_set is what the SPA shows in the login overlay
        # ("a password is required" vs not). Bool, not the password itself.
        assert isinstance(body["auth_password_set"], bool)
        assert body["auth_password_set"] is False  # default conftest config
        assert body["boot_autostart_enabled"] is False  # nothing enabled yet

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

    def test_claude_models_available_includes_fable(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        # Catalog display order, then the legacy-accepted Haiku (#883).
        assert body["claude"]["models_available"] == [
            "sonnet", "opus", "fable", "haiku"
        ]

    def test_antigravity_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        ag = body["antigravity"]
        assert set(ag) == {"skip_permissions", "sandbox", "computed_flags"}
        assert isinstance(ag["skip_permissions"], bool)
        assert isinstance(ag["sandbox"], bool)
        # All-default config → the CLI is launched bare.
        assert ag["computed_flags"] == ""

    def test_grok_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        gk = client.get("/api/config").json()["grok"]
        assert set(gk) == {
            "effort",
            "permission_mode",
            "efforts_available",
            "permission_modes_available",
            "computed_flags",
        }
        assert isinstance(gk["efforts_available"], list) and gk["efforts_available"]
        # Default config → high reasoning + auto permission (guard rails
        # intact), the same defaults every other agent's block carries.
        assert gk["effort"] == "high"
        assert gk["permission_mode"] == "auto"
        assert gk["computed_flags"] == "--permission-mode auto --reasoning-effort high"

    def test_board_dispatch_models_offer_grok_only_when_installed(
        self, webapp_client, monkeypatch
    ):
        client, _, _ = webapp_client
        from app.webapp.routers import config as config_router

        monkeypatch.setattr(config_router, "is_installed", lambda _: True)
        body = client.get("/api/config").json()
        assert body["board_dispatch_models"]["grok"] == [
            {"value": "grok-4.5", "label": "4.5", "available": True, "efforts": []}
        ]

        monkeypatch.setattr(config_router, "is_installed", lambda _: False)
        body = client.get("/api/config").json()
        assert body["board_dispatch_models"] == {"grok": []}

    def test_grok_stays_out_of_the_shared_model_catalog(self, webapp_client, monkeypatch):
        """The Coding tab and Life OS read ``model_catalog``; Grok is Board-only."""
        client, _, _ = webapp_client
        from app.webapp.routers import config as config_router

        monkeypatch.setattr(config_router, "is_installed", lambda _: True)
        catalog = client.get("/api/config").json()["model_catalog"]
        assert set(catalog) == {"claude", "codex"}
        assert not [
            item for items in catalog.values() for item in items
            if "grok" in item["value"]
        ]

    def test_codex_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        cx = body["codex"]
        assert set(cx) == {
            "model",
            "effort",
            "model_efforts",
            "models_available",
            "permission_mode",
            "efforts_available",
            "permission_modes_available",
            "computed_flags",
        }
        assert isinstance(cx["efforts_available"], list) and cx["efforts_available"]
        # Default config → high reasoning + auto permission (sandboxed,
        # no prompts): the safe autopilot, not the all-bypass switch.
        assert cx["effort"] == "xhigh"
        assert cx["model"] == "gpt-6-luna"
        assert cx["permission_mode"] == "auto"
        assert "--ask-for-approval never" in cx["computed_flags"]
        assert "--sandbox workspace-write" in cx["computed_flags"]
        assert "model_reasoning_effort=xhigh" in cx["computed_flags"]
        assert "--model gpt-6-luna" in cx["computed_flags"]
        astra = next(m for m in cx["models_available"] if m["label"] == "Astra")
        assert astra["available"] is True

    def test_copilot_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        cp = body["copilot"]
        # No model/models_available (issue #1017): the launcher sends no
        # --model for Copilot, so the block carries only the allow-all
        # toggle and the flags it composes.
        assert set(cp) == {"skip_permissions", "computed_flags"}
        assert isinstance(cp["skip_permissions"], bool)
        # Default config → the CLI is launched bare.
        assert cp["computed_flags"] == ""

    def test_pi_block_shape(self, webapp_client):
        client, _, _ = webapp_client
        body = client.get("/api/config").json()
        pi = body["pi"]
        assert set(pi) == {
            "model",
            "effort",
            "model_efforts",
            "default_effort",
            "trust_mode",
            "models_available",
            "efforts_available",
            "trust_modes_available",
            "computed_flags",
        }
        assert (pi["effort"], pi["default_effort"], pi["model_efforts"]) == ("high", "high", {})
        # The catalog includes every current subscription-safe family and an
        # honestly disabled Astra rollout entry.
        assert isinstance(pi["models_available"], list) and pi["models_available"]
        values = [m["value"] for m in pi["models_available"]]
        labels = [m["label"] for m in pi["models_available"]]
        assert pi["model"] in values
        assert labels == ["Opus", "Sonnet", "Fable", "Luna", "Terra", "Sol", "Astra"]
        assert pi["effort"] in pi["efforts_available"]
        assert pi["trust_mode"] in pi["trust_modes_available"]
        # Default config → Opus on the claude-agent-sdk subscription path,
        # high thinking, project trust on. Explicit provider/model always —
        # never bare, never the native billing provider.
        assert pi["computed_flags"] == (
            "--provider claude-agent-sdk --model claude-agent-sdk/claude-opus-5 "
            "--thinking high --approve"
        )


class TestBootAutostart:
    """POST /api/settings/boot-autostart — issue #456 part 1/2."""

    def test_enable_then_get_config_reflects_it(
        self, webapp_client, _isolated_startup_dir
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/settings/boot-autostart", json={"enabled": True}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "boot_autostart_enabled": True}
        assert client.get("/api/config").json()["boot_autostart_enabled"] is True
        assert (
            _isolated_startup_dir / boot_autostart_mod.STARTUP_BAT_NAME
        ).is_file()

    def test_disable_removes_it(self, webapp_client, _isolated_startup_dir):
        client, _, _ = webapp_client
        client.post("/api/settings/boot-autostart", json={"enabled": True})
        resp = client.post(
            "/api/settings/boot-autostart", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "boot_autostart_enabled": False}
        assert client.get("/api/config").json()["boot_autostart_enabled"] is False
        assert not (
            _isolated_startup_dir / boot_autostart_mod.STARTUP_BAT_NAME
        ).exists()

    def test_disable_when_never_enabled_is_a_noop(
        self, webapp_client, _isolated_startup_dir
    ):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/settings/boot-autostart", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "boot_autostart_enabled": False}


class TestOutboxScratchRoots:
    """Settings' Chat file folders (#1748): POST /api/config checks each
    folder on write; the loader stays lenient about one that has gone."""

    def _post(self, client, roots):
        return client.post("/api/config", json={"outbox_scratch_roots": roots})

    def test_round_trips_as_typed_and_reaches_disk(self, webapp_client, tmp_path, monkeypatch):
        from src.webapp_config import load_webapp_config

        client, app, _ = webapp_client
        drop = tmp_path / "Drop"
        drop.mkdir()
        monkeypatch.setenv("OUTBOX_TEST_BASE", str(tmp_path))
        typed = ["  %OUTBOX_TEST_BASE%/Drop  ", str(drop / ".." / "Drop")]
        # The two spellings are the same folder: a duplicate, refused.
        assert self._post(client, typed).status_code == 400
        resp = self._post(client, [typed[0], "  "])
        assert resp.status_code == 200
        # The typed text is stored (trimmed, %VAR% kept), never the resolved path.
        assert app.state.webapp_config.outbox_scratch_roots == ["%OUTBOX_TEST_BASE%/Drop"]
        assert client.get("/api/config").json()["outbox_scratch_roots"] == ["%OUTBOX_TEST_BASE%/Drop"]
        assert load_webapp_config(apply_env_override=False).outbox_scratch_roots == [
            "%OUTBOX_TEST_BASE%/Drop"
        ]

    def test_default_is_in_the_get_payload(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/config").json()["outbox_scratch_roots"] == ["%TEMP%/claude"]

    @pytest.mark.parametrize(
        "bad, message",
        [
            (r"\\host\share", "network and device"),
            ("//host/share", "network and device"),
            (r"\\?\C:\x", "network and device"),
            ("{drive_root}", "whole drive"),
            ("{missing}", "does not exist"),
            ("{a_file}", "file, not a folder"),
        ],
    )
    def test_a_bad_entry_is_refused_in_its_own_words_and_saves_nothing(
        self, webapp_client, tmp_path, bad, message
    ):
        client, app, _ = webapp_client
        good = tmp_path / "good"
        good.mkdir()
        a_file = tmp_path / "plain.txt"
        a_file.write_text("x", encoding="utf-8")
        assert self._post(client, [str(good)]).status_code == 200
        bad = bad.format(
            drive_root=Path(tmp_path.anchor), missing=tmp_path / "gone", a_file=a_file
        )
        resp = self._post(client, [str(good), bad])
        assert resp.status_code == 400
        assert message in resp.json()["detail"]
        assert app.state.webapp_config.outbox_scratch_roots == [str(good)]

    def test_a_duplicate_is_refused_by_case_and_separator(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        folder = tmp_path / "Dup"
        folder.mkdir()
        resp = self._post(client, [str(folder), str(folder).upper().replace("\\", "/")])
        assert resp.status_code == 400
        assert "already in the list" in resp.json()["detail"]

    def test_at_least_one_folder_must_stay(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        for empty in ([], ["", "   "]):
            resp = self._post(client, empty)
            assert resp.status_code == 400
            assert "at least one folder" in resp.json()["detail"]
        assert app.state.webapp_config.outbox_scratch_roots == ["%TEMP%/claude"]

    @pytest.mark.parametrize("body", ["a-string", {"a": 1}, None, 5, [1, 2], [["x"]]])
    def test_a_non_list_of_strings_never_reaches_the_config(self, webapp_client, body):
        client, app, _ = webapp_client
        assert self._post(client, body).status_code == 400
        assert app.state.webapp_config.outbox_scratch_roots == ["%TEMP%/claude"]

    def test_a_since_deleted_folder_still_loads_and_other_settings_still_save(
        self, webapp_client, tmp_path
    ):
        import shutil

        client, app, _ = webapp_client
        folder = tmp_path / "soon-gone"
        folder.mkdir()
        assert self._post(client, [str(folder)]).status_code == 200
        shutil.rmtree(folder)
        # The write-time rules are the router's: load, and an unrelated save,
        # must not trip over the missing folder.
        resp = client.post("/api/config", json={"claude_verbose": False})
        assert resp.status_code == 200
        assert app.state.webapp_config.outbox_scratch_roots == [str(folder)]

    def test_a_stored_folder_that_has_gone_does_not_block_editing_the_others(
        self, webapp_client, tmp_path
    ):
        import shutil

        client, app, _ = webapp_client
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert self._post(client, [str(a), str(b)]).status_code == 200
        shutil.rmtree(a)
        # Removing b keeps the gone a; only a NEW entry is checked.
        assert self._post(client, [str(a)]).status_code == 200
        fresh = tmp_path / "c"
        fresh.mkdir()
        assert self._post(client, [str(a), str(fresh)]).status_code == 200
        assert self._post(client, [str(a), str(tmp_path / "never")]).status_code == 400

    def test_adding_a_stored_folder_again_is_a_duplicate(self, webapp_client, tmp_path):
        client, app, _ = webapp_client
        folder = tmp_path / "once"
        folder.mkdir()
        assert self._post(client, [str(folder)]).status_code == 200
        resp = self._post(client, [str(folder), str(folder)])
        assert resp.status_code == 400
        assert "already in the list" in resp.json()["detail"]
        assert app.state.webapp_config.outbox_scratch_roots == [str(folder)]
