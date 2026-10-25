"""Which catalog models the model dropdowns offer, and renamed model ids.

The setting is ``WebappConfig.hidden_models``: provider-qualified values
(``codex:gpt-5.6-sol``) the owner switched off in Settings. It is a *hidden*
list so a model newly added to the catalog shows by default. The Coding
Projects picker, the Life OS Skills picker and the Board dispatch picker all
filter on it; a launch or resume that names a model explicitly is never
filtered, so a running session keeps its own model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Iterable, List, Optional, Sequence

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
