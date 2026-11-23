"""``request_budgets``: an owner-tunable override table that can never break the load."""

from __future__ import annotations

import json
from pathlib import Path

from src.request_health import BUDGETS, class_names
from src.request_timing import Budget, resolve_budgets
from src.webapp_config import WebappConfig, load_webapp_config

SAMPLE = Path(__file__).resolve().parents[3] / "config" / "webapp_config.sample.json"


def _load(tmp_path: Path, raw: dict) -> WebappConfig:
    target = tmp_path / "webapp_config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return load_webapp_config(target, apply_env_override=False)


def test_default_is_empty_so_every_default_budget_applies(tmp_path):
    assert _load(tmp_path, {}).request_budgets == {}


def test_a_valid_override_survives_the_load(tmp_path):
    cfg = _load(tmp_path, {"request_budgets": {"board": [1, 3]}})
    assert cfg.request_budgets == {"board": [1.0, 3.0]}
    assert resolve_budgets(BUDGETS, cfg.request_budgets)["board"] == Budget(1.0, 3.0)


def test_a_bad_entry_is_dropped_and_the_rest_load(tmp_path):
    cfg = _load(tmp_path, {"request_budgets": {"board": [3, 1], "nope": [1, 2], "send": [6, 12]}})
    assert cfg.request_budgets == {"send": [6.0, 12.0]}


def test_a_non_object_value_loads_as_the_defaults(tmp_path):
    assert _load(tmp_path, {"request_budgets": "fast"}).request_budgets == {}


def test_the_sample_documents_the_field_with_every_class_named():
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert raw["request_budgets"] == {}
    comment = raw["_comment_request_budgets"]
    assert all(name in comment for name in class_names())
