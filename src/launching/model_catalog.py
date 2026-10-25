"""Canonical coding-harness model catalog.

Model ids are deliberately explicit and subscription-path aware.  Refresh the
catalog with the commands documented in README.md; unavailable rollout models
remain visible to the UI but are never accepted by a launch builder.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ``default_effort`` is the model's own documented default (Anthropic's effort
# page: "Claude Opus 5.5 and Claude Haiku 5.5 default to medium", every other
# model to high), so sending it is the same as sending nothing. The ``opus``
# alias is Opus 5.5 today; ``tests/unit/launching/test_effort_resolver.py`` pins the value, so
# an alias that moves to another Opus forces a deliberate edit here.
CLAUDE_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "sonnet": {
        "label": "Sonnet", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "high", "available": True,
    },
    "opus": {
        "label": "Opus", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "medium", "available": True,
    },
    "fable": {
        "label": "Fable", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "high", "available": True,
    },
}

# Models accepted for old configs and direct launches but not offered by the
# selectors, so they have no ``CLAUDE_MODEL_SPECS`` row: haiku's default beside
# the legacy tuple it belongs to (``webapp_config.LEGACY_CLAUDE_MODELS``).
LEGACY_CLAUDE_DEFAULT_EFFORTS: Dict[str, str] = {"haiku": "medium"}

# Ids from ``codex debug models`` (CLI 0.164.0). Luna 6.0 replaced Luna 5.6;
# ``RENAMED_CODEX_MODELS`` below carries a saved id over.
CODEX_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "gpt-6-luna": {
        "label": "Luna", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "xhigh", "available": True,
    },
    "gpt-5.6-terra": {
        "label": "Terra", "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
        "default_effort": "medium", "available": True,
    },
    "gpt-5.6-sol": {
        "label": "Sol", "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
        "default_effort": "low", "available": True,
    },
    "gpt-6-astra": {
        "label": "Astra", "efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "medium", "available": True,
    },
}

# Pi is intentionally restricted to the two subscription-backed providers.
# ``default_effort`` is ``high`` throughout: nothing is published for
# Pi's providers, and Pi pins Opus 5 (not 5.5), whose documented default is high.
# Pi's ``--thinking`` ladder is ``VALID_PI_EFFORTS``, so there is no per-spec
# ``efforts`` list.
# Never add the native ``anthropic`` provider here: it uses metered API credit.
PI_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "claude-opus-5": {
        "label": "Opus", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-opus-5",
        "default_effort": "high", "available": True,
    },
    "claude-sonnet-5": {
        "label": "Sonnet", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-sonnet-5",
        "default_effort": "high", "available": True,
    },
    "claude-fable-5": {
        "label": "Fable", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-fable-5",
        "default_effort": "high", "available": True,
    },
    "gpt-5.6-luna": {
        "label": "Luna", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-luna",
        "default_effort": "high", "available": True,
    },
    "gpt-5.6-terra": {
        "label": "Terra", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-terra",
        "default_effort": "high", "available": True,
    },
    "gpt-5.6-sol": {
        "label": "Sol", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-sol",
        "default_effort": "high", "available": True,
    },
    "gpt-6-astra": {
        "label": "Astra", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-6-astra",
        "default_effort": "high", "available": False,
        "unavailable_reason": "Not advertised by the installed Pi Codex provider yet",
    },
}

# Grok Build lists one model and takes no ``--model`` (flags carry the Coding
# tab's effort and permission mode), so this row only names the Board dispatch
# value ``grok:grok-4.5``. It stays out of the shared Claude/Codex catalog on
# purpose: the Coding tab launches Grok its own way and Life OS rejects it.
GROK_DISPATCH_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "grok-4.5": {"label": "4.5", "available": True},
}

# GitHub Copilot has no entry here on purpose. Its model
# list was removed rather than refreshed: `copilot help config` reports
# what the CLI accepts, while what a *launch* gets is decided by the
# account's entitlement, so a catalogue here could only ever produce a
# claim the launcher cannot keep. Copilot's `/model` picker is the live,
# accurate list; the launcher sends no `--model` for it.


def available_values(specs: Dict[str, Dict[str, Any]]) -> Tuple[str, ...]:
    """Return launchable ids in authored display order."""
    return tuple(value for value, spec in specs.items() if spec.get("available"))


def catalog_payload(specs: Dict[str, Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Return JSON-safe model metadata for selectors."""
    return [
        {
            "value": value,
            "label": spec["label"],
            "available": bool(spec.get("available")),
            "efforts": list(spec.get("efforts", ())),
            **({"default_effort": spec["default_effort"]} if spec.get("default_effort") else {}),
            **({"unavailable_reason": spec["unavailable_reason"]} if spec.get("unavailable_reason") else {}),
        }
        for value, spec in specs.items()
    ]


# ------------------------------------------------- dropdown visibility
# Which catalog models the model dropdowns offer, and renamed model ids.
# 
# The setting is ``WebappConfig.hidden_models``: provider-qualified values
# (``codex:gpt-5.6-sol``) the owner switched off in Settings. It is a *hidden*
# list so a model newly added to the catalog shows by default. The Coding
# Projects picker, the Life OS Skills picker and the Board dispatch picker all
# filter on it; a launch or resume that names a model explicitly is never
# filtered, so a running session keeps its own model.

# Codex on a ChatGPT-account sign-in refuses Terra, Sol and Astra with
# ``model_not_found``, so a fresh or upgraded config offers Luna alone.
DEFAULT_HIDDEN_MODELS = (
    "codex:gpt-5.6-terra",
    "codex:gpt-5.6-sol",
    "codex:gpt-6-astra",
)

# Codex ids the catalog replaced: old id -> its successor. A saved config is
# rewritten on load, because validation refuses an id the catalog dropped.
RENAMED_CODEX_MODELS: Dict[str, str] = {"gpt-5.6-luna": "gpt-6-luna"}


def load_hidden_models(raw: Dict[str, Any]) -> List[str]:
    """The saved list, kept when empty: ``[]`` means "show every model", while
    a missing key (a config from before the setting) gets the default."""
    value = raw.get("hidden_models")
    if not isinstance(value, list):
        return list(DEFAULT_HIDDEN_MODELS)
    return [str(item) for item in value]


def _renamed_choice(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("codex:"):
        model = value.split(":", 1)[1]
        return "codex:" + RENAMED_CODEX_MODELS.get(model, model)
    return value


def migrate_renamed_models(raw: Dict[str, Any]) -> Dict[str, Any]:
    """``raw`` with every renamed Codex id carried to its successor.

    In memory only: the next save writes the new ids. An entry already keyed
    by the successor wins over the renamed one.
    """
    migrated = dict(raw)
    model = migrated.get("codex_model")
    if model in RENAMED_CODEX_MODELS:
        migrated["codex_model"] = RENAMED_CODEX_MODELS[model]
    efforts = migrated.get("codex_model_efforts")
    if isinstance(efforts, dict):
        carried = {RENAMED_CODEX_MODELS.get(k, k): v for k, v in efforts.items()}
        carried.update({k: v for k, v in efforts.items() if k not in RENAMED_CODEX_MODELS})
        migrated["codex_model_efforts"] = carried
    if "coding_model_choice" in migrated:
        migrated["coding_model_choice"] = _renamed_choice(migrated["coding_model_choice"])
    hidden = migrated.get("hidden_models")
    if isinstance(hidden, list):
        migrated["hidden_models"] = [_renamed_choice(value) for value in hidden]
    return migrated


def visible_choice(
    choice: str, hidden: Iterable[str], choices: Sequence[str]
) -> Optional[str]:
    """``choice`` when it is offered, else the first offered model of its agent.

    ``None`` when every model of that agent is hidden: the caller keeps the
    saved value rather than switching agent behind the owner's back.
    """
    hidden_set = set(hidden)
    if choice not in hidden_set:
        return choice
    agent = choice.split(":", 1)[0] + ":"
    return next(
        (c for c in choices if c.startswith(agent) and c not in hidden_set), None
    )


def with_visible_defaults(cfg: Any, choices: Sequence[str]) -> Any:
    """``cfg`` with each saved default that is now hidden moved to a visible one.

    Covers the Coding picker's ``coding_model_choice`` and the provider
    defaults a launch reads (``claude_model``, ``codex_model`` with its
    remembered ``codex_effort``).
    """
    hidden = list(cfg.hidden_models)
    changes: Dict[str, Any] = {}
    for agent, field_name in (("claude", "claude_model"), ("codex", "codex_model")):
        current = f"{agent}:{getattr(cfg, field_name)}"
        moved = visible_choice(current, hidden, choices)
        if moved and moved != current:
            changes[field_name] = moved.split(":", 1)[1]
    if "codex_model" in changes:
        changes["codex_effort"] = cfg.codex_model_efforts.get(
            changes["codex_model"], cfg.codex_effort
        )
    moved = visible_choice(cfg.coding_model_choice, hidden, choices)
    if moved and moved != cfg.coding_model_choice:
        changes["coding_model_choice"] = moved
    return replace(cfg, **changes) if changes else cfg
