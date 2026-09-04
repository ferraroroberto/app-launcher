"""Issue #722: load and save are derived from ``WebappConfig``'s field list.

The ~55 settings used to be enumerated three times — as dataclass fields, as
a per-field ``raw.get(...)`` block in ``load_webapp_config``, and as a payload
dict in ``save_webapp_config``. Adding one setting meant three edits, and
forgetting the load-side one failed *closed*: the field silently sat on its
default with nothing raising.

Both directions now read the dataclass, so that particular drift is
structurally impossible. What still needs pinning is the behaviour the
generic loop has to preserve, because getting it wrong is silent in exactly
the same way:

- every declared field survives a save → load round trip;
- a ``default_factory`` field treats a falsy on-disk value as absent and
  re-derives its default (an empty ``projects_dir`` means "never set");
- a literal-default field keeps a falsy value as authored, because that is
  how features here are switched off (an empty ``voice_transcriber_url``
  hides the 🎤 button). Collapsing these two disciplines would silently
  re-enable whatever the user turned off.
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields

import pytest

from src.webapp_config import (
    WebappConfig,
    load_webapp_config,
    save_webapp_config,
)


def _write(tmp_path, payload: dict):
    target = tmp_path / "webapp_config.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_every_field_survives_a_save_load_round_trip(tmp_path):
    """A save writes every declared field and a load reads every declared
    field back — the property the triple enumeration kept breaking."""
    cfg = WebappConfig()
    target = tmp_path / "webapp_config.json"

    save_webapp_config(cfg, target)
    written = json.loads(target.read_text(encoding="utf-8"))
    declared = {spec.name for spec in dataclass_fields(WebappConfig)}
    assert set(written) == declared, (
        "the on-disk payload and the dataclass have drifted apart"
    )

    reloaded = load_webapp_config(target, apply_env_override=False)
    assert reloaded == cfg


def test_non_default_values_survive_a_round_trip(tmp_path):
    """Round-tripping identity on the defaults alone would pass even if a
    field were pinned to its default on load, so re-check with values that
    differ from every default."""
    cfg = WebappConfig(
        github_owner="someone-else",
        claude_verbose=False,
        chief_worker_cap=7,
        projects_ignore=["skip-me", "and-me"],
        tailnet_allowlist=["10.0.0.0/8"],
        secrets={"api": "shh"},
        api_tokens=[{"id": "t1", "label": "phone", "scope": "*"}],
    )
    target = tmp_path / "webapp_config.json"

    save_webapp_config(cfg, target)

    assert load_webapp_config(target, apply_env_override=False) == cfg


def test_blank_factory_field_falls_back_to_its_default(tmp_path):
    """``projects_dir`` and friends carry a ``default_factory``: a blank on
    disk means "never set", not "scan from an empty path"."""
    target = _write(tmp_path, {"projects_dir": "", "projects_ignore": None})

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.projects_dir == WebappConfig().projects_dir
    assert cfg.projects_ignore == []


def test_blank_literal_default_field_stays_blank(tmp_path):
    """The opposite discipline, and the one a naive "falsy means absent"
    loop would silently break: an empty URL/token is how these features are
    switched off, so it must not be re-derived from the default."""
    target = _write(
        tmp_path,
        {"voice_transcriber_url": "", "auth_token": "", "github_owner": ""},
    )

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.voice_transcriber_url == ""
    assert cfg.auth_token == ""
    assert cfg.github_owner == ""


def test_unknown_keys_are_ignored(tmp_path):
    """Only declared fields are read — an old or hand-added key must not
    reach the constructor as an unexpected kwarg."""
    target = _write(tmp_path, {"host": "127.0.0.1", "retired_setting": 1})

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.host == "127.0.0.1"


def test_unusable_number_falls_back_instead_of_raising(tmp_path):
    """A hand-edited config with a null in an int field degrades to the
    declared default rather than taking the whole webapp down on import."""
    target = _write(tmp_path, {"chief_worker_cap": None, "port": "8500"})

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.chief_worker_cap == WebappConfig().chief_worker_cap
    assert cfg.port == 8500  # numeric strings still coerce


def test_non_boolean_bool_field_falls_back_instead_of_truthy_coercion(tmp_path):
    """A hand-edited config with a string in a bool field must not be
    coerced by Python truthiness — ``bool("false")`` is ``True`` (issue
    #755), which would silently flip a feature the user just turned off.
    The int branch already falls back to the declared default on an
    unusable value; the bool branch must match that discipline.
    """
    target = _write(
        tmp_path,
        # claude_debug defaults False; claude_verbose defaults True.
        {"claude_debug": "false", "claude_verbose": "no"},
    )

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.claude_debug == WebappConfig().claude_debug
    assert cfg.claude_verbose == WebappConfig().claude_verbose


def test_legacy_webhook_secrets_key_still_loads(tmp_path):
    """``secrets`` shipped as ``webhook_secrets`` before #72 generalized it;
    old files in the wild still spell it that way."""
    target = _write(tmp_path, {"webhook_secrets": {"hook": "value"}})

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.secrets == {"hook": "value"}


def test_malformed_api_token_records_are_dropped(tmp_path):
    """A half-written token record fails closed — it can never become a
    credential that matches something."""
    target = _write(
        tmp_path, {"api_tokens": [{"id": "ok"}, "not-a-record", None]}
    )

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.api_tokens == [{"id": "ok"}]


def test_pre_model_codex_config_adopts_luna_extra_high(tmp_path):
    target = _write(tmp_path, {"codex_effort": "high"})

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.codex_model == "gpt-5.6-luna"
    assert cfg.codex_effort == "xhigh"
    assert cfg.codex_model_efforts["gpt-5.6-luna"] == "xhigh"


@pytest.mark.parametrize(
    ("old_model", "new_model"),
    [
        ("claude-opus-4-8", "claude-opus-5"),
        ("claude-sonnet-4-6", "claude-sonnet-5"),
        ("gpt-5.5", "gpt-5.6-sol"),
    ],
)
def test_stale_pi_model_ids_migrate(tmp_path, old_model, new_model):
    target = _write(tmp_path, {"pi_model": old_model})

    cfg = load_webapp_config(target, apply_env_override=False)

    assert cfg.pi_model == new_model


@pytest.mark.parametrize("payload", ["[]", '"a string"', "42"])
def test_non_object_config_falls_back_to_defaults(tmp_path, payload):
    """The file parsing as valid JSON says nothing about it being an object;
    a list or scalar must degrade the same way unreadable JSON does."""
    target = tmp_path / "webapp_config.json"
    target.write_text(payload, encoding="utf-8")

    assert load_webapp_config(target, apply_env_override=False) == WebappConfig()
