"""Canonical coding-harness model catalog.

Model ids are deliberately explicit and subscription-path aware.  Refresh the
catalog with the commands documented in README.md; unavailable rollout models
remain visible to the UI but are never accepted by a launch builder.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


# ``default_effort`` is the model's own documented default (Anthropic's effort
# page: "Claude Opus 5.5 and Claude Haiku 5.5 default to medium", every other
# model to high), so sending it is the same as sending nothing. The ``opus``
# alias is Opus 5.5 today; ``tests/test_effort_resolver.py`` pins the value, so
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

CODEX_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "gpt-5.6-luna": {
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
# Never add the native ``anthropic`` provider here: it uses metered API credit.
PI_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "claude-opus-5": {
        "label": "Opus", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-opus-5", "available": True,
    },
    "claude-sonnet-5": {
        "label": "Sonnet", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-sonnet-5", "available": True,
    },
    "claude-fable-5": {
        "label": "Fable", "provider": "claude-agent-sdk",
        "model_arg": "claude-agent-sdk/claude-fable-5", "available": True,
    },
    "gpt-5.6-luna": {
        "label": "Luna", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-luna", "available": True,
    },
    "gpt-5.6-terra": {
        "label": "Terra", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-terra", "available": True,
    },
    "gpt-5.6-sol": {
        "label": "Sol", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-5.6-sol", "available": True,
    },
    "gpt-6-astra": {
        "label": "Astra", "provider": "openai-codex",
        "model_arg": "openai-codex/gpt-6-astra", "available": False,
        "unavailable_reason": "Not advertised by the installed Pi Codex provider yet",
    },
}

# GitHub Copilot has no entry here on purpose (issue #1017). Its model
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
