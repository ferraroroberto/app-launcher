"""Canonical coding-harness model catalog.

Model ids are deliberately explicit and subscription-path aware.  Refresh the
catalog with the commands documented in README.md; unavailable rollout models
remain visible to the UI but are never accepted by a launch builder.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


CLAUDE_MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "sonnet": {"label": "Sonnet", "efforts": ("low", "medium", "high", "xhigh", "max"), "available": True},
    "opus": {"label": "Opus", "efforts": ("low", "medium", "high", "xhigh", "max"), "available": True},
    "fable": {"label": "Fable", "efforts": ("low", "medium", "high", "xhigh", "max"), "available": True},
}

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

COPILOT_MODELS: Tuple[str, ...] = (
    "claude-sonnet-5", "claude-sonnet-4.6", "claude-sonnet-4.5",
    "claude-haiku-4.5", "claude-fable-5", "claude-opus-5",
    "claude-opus-4.8", "claude-opus-4.8-fast", "claude-opus-4.7",
    "claude-opus-4.6", "claude-opus-4.5", "gpt-5.6-sol",
    "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
    "gpt-5.3-codex", "gpt-5.4-mini", "gpt-5-mini",
    "gemini-3.1-pro-preview", "gemini-3.5-flash", "kimi-k2.7-code",
)


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
            **({"unavailable_reason": spec["unavailable_reason"]} if spec.get("unavailable_reason") else {}),
        }
        for value, spec in specs.items()
    ]
