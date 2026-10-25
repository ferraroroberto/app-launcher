"""Loading hidden_models and renamed Codex ids from a saved config."""

from __future__ import annotations

import json

from src.launching.model_catalog import migrate_renamed_models, visible_choice
from src.webapp_config import VALID_CODING_MODEL_CHOICES, load_webapp_config


def test_saved_luna_5_6_loads_as_luna_6(tmp_path):
    path = tmp_path / "webapp_config.json"
    path.write_text(json.dumps({
        "codex_model": "gpt-5.6-luna",
        "codex_effort": "high",
        "codex_model_efforts": {"gpt-5.6-luna": "high", "gpt-5.6-terra": "ultra"},
        "coding_model_choice": "codex:gpt-5.6-luna",
    }), encoding="utf-8")
    cfg = load_webapp_config(path, apply_env_override=False)
    assert cfg.codex_model == "gpt-6-luna"
    assert cfg.coding_model_choice == "codex:gpt-6-luna"
    assert cfg.codex_model_efforts["gpt-6-luna"] == "high"
    assert "gpt-5.6-luna" not in cfg.codex_model_efforts


def test_successor_entry_wins_over_the_renamed_one():
    raw = migrate_renamed_models({"codex_model_efforts": {"gpt-6-luna": "low", "gpt-5.6-luna": "max"}})
    assert raw["codex_model_efforts"] == {"gpt-6-luna": "low"}


def test_upgraded_config_moves_a_hidden_codex_default_to_luna(tmp_path):
    path = tmp_path / "webapp_config.json"
    path.write_text(json.dumps({
        "codex_model": "gpt-5.6-sol",
        "codex_effort": "ultra",
        "coding_model_choice": "codex:gpt-5.6-sol",
    }), encoding="utf-8")
    cfg = load_webapp_config(path, apply_env_override=False)
    assert cfg.codex_model == "gpt-6-luna"
    assert cfg.coding_model_choice == "codex:gpt-6-luna"
    assert cfg.codex_effort in ("low", "medium", "high", "xhigh", "max")


def test_every_model_of_an_agent_hidden_keeps_the_saved_value():
    hidden = [c for c in VALID_CODING_MODEL_CHOICES if c.startswith("codex:")]
    assert visible_choice("codex:gpt-6-luna", hidden, VALID_CODING_MODEL_CHOICES) is None
    assert visible_choice("claude:opus", hidden, VALID_CODING_MODEL_CHOICES) == "claude:opus"
