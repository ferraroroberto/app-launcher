"""#1711: every model launches at its own effort.

One resolver, ``effective_effort(agent, model, cfg)``: the owner's override for
that model, else the catalog ``default_effort``, else ``high``. These pin the
rule itself, the argv every Claude model gets, the resume paths, the
migration of the old global ``claude_effort``, and that Codex — moved onto the
same resolver — still produces the argv it always did. Step 2/2 (#1713) puts
Pi on the same resolver: overrides only, the legacy ``pi_effort`` migrated.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.agents import resume_command_for
from src.launch_flags import (
    build_claude_flags,
    build_codex_flags,
    build_pi_flags,
    build_resume_flags,
    claude_model_family,
    default_effort,
    effective_effort,
)
from src.model_catalog import CLAUDE_MODEL_SPECS, CODEX_MODEL_SPECS, PI_MODEL_SPECS
from src.webapp_config import (
    VALID_CLAUDE_MODELS,
    VALID_PI_MODELS,
    WebappConfig,
    load_webapp_config,
    save_webapp_config,
)


def _cfg(**fields) -> WebappConfig:
    return replace(WebappConfig(), **fields)


def _effort_arg(flags: str) -> str:
    """The value after ``--effort``, or ``""`` when the flag is absent."""
    parts = flags.split()
    return parts[parts.index("--effort") + 1] if "--effort" in parts else ""


class TestCatalogDefaults:
    def test_claude_defaults_follow_the_documented_model_defaults(self):
        # Anthropic's effort page: Opus 5.5 and Haiku 5.5 default to medium,
        # every other model to high. The `opus` alias is Opus 5.5 today; if it
        # moves to another Opus this pin forces a deliberate catalog edit.
        assert CLAUDE_MODEL_SPECS["opus"]["default_effort"] == "medium"
        assert CLAUDE_MODEL_SPECS["sonnet"]["default_effort"] == "high"
        assert CLAUDE_MODEL_SPECS["fable"]["default_effort"] == "high"
        assert default_effort("claude", "haiku") == "medium"  # legacy entry, no spec row

    def test_every_default_is_one_the_model_accepts(self):
        for model, spec in CLAUDE_MODEL_SPECS.items():
            assert spec["default_effort"] in spec["efforts"], model


class TestResolver:
    def test_catalog_default_when_nothing_is_overridden(self):
        cfg = _cfg()
        assert effective_effort("claude", "opus", cfg) == "medium"
        assert effective_effort("claude", "sonnet", cfg) == "high"
        assert effective_effort("claude", "fable", cfg) == "high"
        assert effective_effort("claude", "haiku", cfg) == "medium"

    def test_override_wins_for_its_model_only(self):
        cfg = _cfg(claude_model_efforts={"opus": "max"})
        assert effective_effort("claude", "opus", cfg) == "max"
        assert effective_effort("claude", "sonnet", cfg) == "high"

    def test_off_is_a_valid_override(self):
        cfg = _cfg(claude_model_efforts={"sonnet": "off"})
        assert effective_effort("claude", "sonnet", cfg) == "off"

    def test_an_unsupported_override_is_ignored(self):
        cfg = _cfg(claude_model_efforts={"opus": "ultra"})
        assert effective_effort("claude", "opus", cfg) == "medium"

    def test_unknown_model_falls_back_to_high(self):
        assert effective_effort("claude", "no-such-model", _cfg()) == "high"
        assert effective_effort("codex", "no-such-model", _cfg()) == "high"

    def test_unknown_agent_is_refused(self):
        with pytest.raises(ValueError):
            effective_effort("grok", "grok-4.5", _cfg())

    def test_codex_reads_its_own_per_model_dict(self):
        cfg = _cfg(codex_model_efforts={"gpt-5.6-sol": "high"})
        assert effective_effort("codex", "gpt-5.6-sol", cfg) == "high"
        assert effective_effort("codex", "gpt-5.6-terra", cfg) == "medium"  # catalog


class TestClaudeArgv:
    @pytest.mark.parametrize(
        "model,expected", [("opus", "medium"), ("sonnet", "high"), ("fable", "high")]
    )
    def test_each_model_launches_at_its_own_default(self, model, expected):
        assert _effort_arg(build_claude_flags(_cfg(claude_model=model))) == expected

    def test_a_model_override_gets_that_models_effort_not_the_selected_ones(self):
        cfg = _cfg(claude_model="opus", claude_model_efforts={"opus": "max"})
        flags = build_claude_flags(cfg, model_override="sonnet")
        assert "--model sonnet" in flags
        assert _effort_arg(flags) == "high"
        assert _effort_arg(build_claude_flags(cfg)) == "max"

    def test_off_sends_no_effort_flag(self):
        cfg = _cfg(claude_model_efforts={"opus": "off"})
        assert "--effort" not in build_claude_flags(cfg)

    def test_legacy_haiku_uses_its_own_default(self):
        assert _effort_arg(build_claude_flags(_cfg(), model_override="haiku")) == "medium"

    def test_a_channel_session_uses_the_channel_models_effort(self):
        cfg = _cfg(claude_model="opus")
        flags = build_claude_flags(cfg, model_override="sonnet", channel_settings="C:/x/s.json")
        assert _effort_arg(flags) == "high"


class TestResume:
    def test_resume_uses_the_resolver_for_the_model_it_resumes_with(self):
        cfg = _cfg(claude_model_efforts={"fable": "xhigh"})
        picker = build_resume_flags(cfg, "claude", model_override="fable")
        assert _effort_arg(picker) == "xhigh"
        assert _effort_arg(build_resume_flags(cfg, "claude", model_override="sonnet")) == "high"
        assert _effort_arg(build_resume_flags(cfg, "claude")) == "medium"  # selected: opus

    def test_keep_model_resolves_the_recorded_models_family(self):
        cfg = _cfg(claude_model="fable", claude_model_efforts={"sonnet": "low"})
        flags = build_resume_flags(
            cfg, "claude", keep_model=True, session_id="s", effort_model="claude-sonnet-5-5"
        )
        assert "--model" not in flags
        assert _effort_arg(flags) == "low"
        opus = build_resume_flags(
            cfg, "claude", keep_model=True, session_id="s", effort_model="claude-opus-5-5"
        )
        assert _effort_arg(opus) == "medium"

    @pytest.mark.parametrize("recorded", ["", None, "<synthetic>", "gpt-5.6-sol"])
    def test_keep_model_with_an_unknown_recorded_model_omits_the_effort(self, recorded):
        cfg = _cfg(claude_model_efforts={"opus": "max"})
        flags = build_resume_flags(
            cfg, "claude", keep_model=True, session_id="s", effort_model=recorded
        )
        assert "--model" not in flags and "--effort" not in flags

    def test_model_family_is_read_from_the_recorded_id(self):
        assert claude_model_family("claude-opus-5-5-20260101") == "opus"
        assert claude_model_family("claude-haiku-5-5") == "haiku"
        assert claude_model_family("gpt-5.6-sol") == ""
        assert claude_model_family("") == ""


class TestCodexGolden:
    """Codex moved onto the resolver with byte-identical argv (#1711)."""

    GOLDEN_FRESH = {
        "gpt-6-luna": "--model gpt-6-luna --ask-for-approval never --sandbox workspace-write -c model_reasoning_effort=xhigh",
        "gpt-5.6-terra": "--model gpt-5.6-terra --ask-for-approval never --sandbox workspace-write -c model_reasoning_effort=medium",
        "gpt-5.6-sol": "--model gpt-5.6-sol --ask-for-approval never --sandbox workspace-write -c model_reasoning_effort=low",
        "gpt-6-astra": "--model gpt-6-astra --ask-for-approval never --sandbox workspace-write -c model_reasoning_effort=medium",
    }

    @pytest.mark.parametrize("model", sorted(GOLDEN_FRESH))
    def test_fresh_launch_for_every_model(self, model):
        cfg = _cfg(codex_model="gpt-6-luna", codex_effort="xhigh")
        assert build_codex_flags(cfg, model_override=model) == self.GOLDEN_FRESH[model]

    @pytest.mark.parametrize("model", sorted(GOLDEN_FRESH))
    def test_resume_for_every_model(self, model):
        flags = build_resume_flags(_cfg(), "codex", model_override=model)
        effort = CODEX_MODEL_SPECS[model]["default_effort"]
        assert flags == f"resume --model {model} -c model_reasoning_effort={effort}"

    def test_a_remembered_effort_on_a_non_selected_model_is_launched(self):
        cfg = _cfg(codex_model_efforts={**_cfg().codex_model_efforts, "gpt-5.6-sol": "high"})
        assert build_codex_flags(cfg, model_override="gpt-5.6-sol").endswith("=high")

    def test_unknown_model_sends_no_effort(self):
        flags = build_codex_flags(_cfg(), model_override="no-such-model")
        assert "model_reasoning_effort" not in flags and "--model" not in flags


class TestConfigRoundTrip:
    def test_overrides_save_and_reload(self, tmp_path):
        target = tmp_path / "webapp_config.json"
        save_webapp_config(_cfg(claude_model_efforts={"opus": "high", "sonnet": "off"}), target)
        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["claude_model_efforts"] == {"opus": "high", "sonnet": "off"}
        assert "claude_effort" not in written
        reloaded = load_webapp_config(target, apply_env_override=False)
        assert reloaded.claude_model_efforts == {"opus": "high", "sonnet": "off"}

    def test_invalid_entries_are_dropped_on_load(self, tmp_path):
        target = tmp_path / "webapp_config.json"
        target.write_text(json.dumps({"claude_model_efforts": {
            "opus": "max", "ghost": "low", "sonnet": "ultra", "fable": 3,
        }}), encoding="utf-8")
        cfg = load_webapp_config(target, apply_env_override=False)
        assert cfg.claude_model_efforts == {"opus": "max"}

    def test_a_non_object_value_loads_as_no_overrides(self, tmp_path):
        target = tmp_path / "webapp_config.json"
        target.write_text(json.dumps({"claude_model_efforts": ["opus"]}), encoding="utf-8")
        assert load_webapp_config(target, apply_env_override=False).claude_model_efforts == {}

    def test_a_hand_edited_codex_effort_still_wins_for_the_selected_model(self, tmp_path):
        target = tmp_path / "webapp_config.json"
        target.write_text(json.dumps({
            "codex_model": "gpt-6-astra", "codex_effort": "high",
            "codex_model_efforts": {"gpt-6-astra": "low"},
        }), encoding="utf-8")
        cfg = load_webapp_config(target, apply_env_override=False)
        assert effective_effort("codex", "gpt-6-astra", cfg) == "high"


class TestMigration:
    def _load(self, tmp_path, payload):
        target = tmp_path / "webapp_config.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return load_webapp_config(target, apply_env_override=False)

    def test_a_legacy_global_high_does_not_land_on_opus(self, tmp_path):
        cfg = self._load(tmp_path, {"claude_model": "opus", "claude_effort": "high"})
        assert cfg.claude_model_efforts == {}
        assert _effort_arg(build_claude_flags(cfg)) == "medium"

    def test_a_deliberate_legacy_value_reaches_every_model(self, tmp_path):
        cfg = self._load(tmp_path, {"claude_effort": "max"})
        assert cfg.claude_model_efforts == {m: "max" for m in VALID_CLAUDE_MODELS}
        for model in ("opus", "sonnet", "fable"):
            assert _effort_arg(build_claude_flags(cfg, model_override=model)) == "max"

    def test_a_legacy_off_is_carried_so_nothing_is_sent_as_before(self, tmp_path):
        cfg = self._load(tmp_path, {"claude_effort": "off"})
        assert "--effort" not in build_claude_flags(cfg)

    def test_an_existing_dict_wins_over_the_legacy_key(self, tmp_path):
        cfg = self._load(tmp_path, {
            "claude_effort": "max", "claude_model_efforts": {"opus": "low"},
        })
        assert cfg.claude_model_efforts == {"opus": "low"}

    def test_the_next_save_drops_the_legacy_key(self, tmp_path):
        cfg = self._load(tmp_path, {"claude_effort": "low"})
        target = tmp_path / "out.json"
        save_webapp_config(cfg, target)
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "claude_effort" not in written
        assert written["claude_model_efforts"]["opus"] == "low"


def _thinking_arg(flags: str) -> str:
    """The value after ``--thinking``, or ``""`` when the flag is absent."""
    parts = flags.split()
    return parts[parts.index("--thinking") + 1] if "--thinking" in parts else ""


class TestPiResolver:
    """#1713: Pi rides the same resolver; its catalog defaults are all ``high``."""

    def test_every_pi_spec_defaults_to_high(self):
        # Nothing is published for Pi's providers, and Pi pins Opus 5 (not 5.5),
        # whose documented default is high.
        assert set(PI_MODEL_SPECS) >= {"claude-opus-5", "gpt-5.6-sol", "gpt-6-astra"}
        for model, spec in PI_MODEL_SPECS.items():
            assert spec["default_effort"] == "high", model
            assert default_effort("pi", model) == "high"

    def test_no_overrides_means_high_for_every_model(self):
        for model in PI_MODEL_SPECS:
            assert effective_effort("pi", model, _cfg()) == "high"

    def test_an_override_wins_for_its_model_only(self):
        cfg = _cfg(pi_model_efforts={"gpt-5.6-sol": "low"})
        assert effective_effort("pi", "gpt-5.6-sol", cfg) == "low"
        assert effective_effort("pi", "claude-opus-5", cfg) == "high"

    def test_off_is_a_valid_pi_override(self):
        cfg = _cfg(pi_model_efforts={"claude-opus-5": "off"})
        assert effective_effort("pi", "claude-opus-5", cfg) == "off"

    def test_an_unsupported_override_is_ignored(self):
        cfg = _cfg(pi_model_efforts={"claude-opus-5": "ultra"})
        assert effective_effort("pi", "claude-opus-5", cfg) == "high"

    def test_unknown_model_falls_back_to_high(self):
        assert effective_effort("pi", "no-such-model", _cfg()) == "high"


class TestPiArgv:
    @pytest.mark.parametrize("model", sorted(VALID_PI_MODELS))
    def test_each_model_launches_at_high_with_no_overrides(self, model):
        flags = build_pi_flags(_cfg(pi_model=model))
        assert _thinking_arg(flags) == "high"
        assert f"--model {PI_MODEL_SPECS[model]['model_arg']}" in flags

    def test_an_override_applies_to_the_selected_model_only(self):
        cfg = _cfg(pi_model="gpt-5.6-sol", pi_model_efforts={"gpt-5.6-sol": "low"})
        assert _thinking_arg(build_pi_flags(cfg)) == "low"
        other = replace(cfg, pi_model="claude-sonnet-5")
        assert _thinking_arg(build_pi_flags(other)) == "high"

    def test_the_effort_comes_before_the_trust_flag_as_it_always_did(self):
        flags = build_pi_flags(_cfg(pi_model_efforts={"claude-opus-5": "medium"}))
        assert flags == (
            "--provider claude-agent-sdk --model claude-agent-sdk/claude-opus-5 "
            "--thinking medium --approve"
        )

    def test_an_unknown_pi_model_falls_back_to_the_default_models_effort(self):
        cfg = _cfg(pi_model="not-a-model", pi_model_efforts={"claude-opus-5": "low"})
        assert _thinking_arg(build_pi_flags(cfg)) == "low"

    def test_resume_uses_the_resolver(self):
        cfg = _cfg(pi_model="gpt-5.6-terra", pi_model_efforts={"gpt-5.6-terra": "xhigh"})
        flags = build_resume_flags(cfg, "pi")
        assert flags.startswith(resume_command_for("pi"))
        assert _thinking_arg(flags) == "xhigh"
        assert _thinking_arg(build_resume_flags(replace(cfg, pi_model="claude-opus-5"), "pi")) == "high"


class TestPiConfigRoundTrip:
    def test_overrides_save_and_reload(self, tmp_path):
        target = tmp_path / "webapp_config.json"
        save_webapp_config(_cfg(pi_model_efforts={"claude-opus-5": "low", "gpt-5.6-sol": "off"}), target)
        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["pi_model_efforts"] == {"claude-opus-5": "low", "gpt-5.6-sol": "off"}
        assert "pi_effort" not in written
        reloaded = load_webapp_config(target, apply_env_override=False)
        assert reloaded.pi_model_efforts == {"claude-opus-5": "low", "gpt-5.6-sol": "off"}

    def test_invalid_entries_are_dropped_on_load(self, tmp_path):
        target = tmp_path / "webapp_config.json"
        target.write_text(json.dumps({"pi_model_efforts": {
            "claude-opus-5": "max", "ghost": "low", "gpt-5.6-sol": "ultra", "claude-sonnet-5": 3,
        }}), encoding="utf-8")
        cfg = load_webapp_config(target, apply_env_override=False)
        assert cfg.pi_model_efforts == {"claude-opus-5": "max"}

    def test_a_non_object_value_loads_as_no_overrides(self, tmp_path):
        target = tmp_path / "webapp_config.json"
        target.write_text(json.dumps({"pi_model_efforts": ["claude-opus-5"]}), encoding="utf-8")
        assert load_webapp_config(target, apply_env_override=False).pi_model_efforts == {}


class TestPiMigration:
    def _load(self, tmp_path, payload):
        target = tmp_path / "webapp_config.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return load_webapp_config(target, apply_env_override=False)

    def test_a_legacy_medium_is_carried_to_every_pi_model(self, tmp_path):
        # The live value: Pi behaviour must not change for an existing config.
        cfg = self._load(tmp_path, {"pi_model": "gpt-5.6-sol", "pi_effort": "medium"})
        assert cfg.pi_model_efforts == {m: "medium" for m in VALID_PI_MODELS}
        for model in VALID_PI_MODELS:
            assert effective_effort("pi", model, cfg) == "medium"
            assert _thinking_arg(build_pi_flags(replace(cfg, pi_model=model))) == "medium"

    def test_a_legacy_high_is_dropped(self, tmp_path):
        cfg = self._load(tmp_path, {"pi_effort": "high"})
        assert cfg.pi_model_efforts == {}
        assert _thinking_arg(build_pi_flags(cfg)) == "high"

    def test_a_legacy_off_is_carried_so_pi_still_thinks_nothing(self, tmp_path):
        cfg = self._load(tmp_path, {"pi_effort": "off"})
        assert _thinking_arg(build_pi_flags(cfg)) == "off"

    def test_an_unknown_legacy_value_is_dropped_not_carried(self, tmp_path):
        assert self._load(tmp_path, {"pi_effort": "ultra"}).pi_model_efforts == {}

    def test_an_existing_dict_wins_over_the_legacy_key(self, tmp_path):
        cfg = self._load(tmp_path, {
            "pi_effort": "medium", "pi_model_efforts": {"claude-opus-5": "low"},
        })
        assert cfg.pi_model_efforts == {"claude-opus-5": "low"}

    def test_the_claude_and_pi_legacy_keys_migrate_independently(self, tmp_path):
        cfg = self._load(tmp_path, {"claude_effort": "high", "pi_effort": "medium"})
        assert cfg.claude_model_efforts == {}
        assert cfg.pi_model_efforts == {m: "medium" for m in VALID_PI_MODELS}

    def test_the_next_save_drops_the_legacy_key(self, tmp_path):
        cfg = self._load(tmp_path, {"pi_effort": "low"})
        target = tmp_path / "out.json"
        save_webapp_config(cfg, target)
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "pi_effort" not in written
        assert written["pi_model_efforts"]["claude-opus-5"] == "low"
