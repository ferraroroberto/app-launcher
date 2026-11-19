"""Named launch presets: a model plus the effort it launches at.

A preset is a shortcut the Coding and Life OS launch dropdowns offer next to
"Default". It lives in ``webapp_config.json`` as a list of
``{id, name, agent, model, effort}`` rows, plus a ``context`` tier on a model
that offers one (Codex); the model, effort and tier come from the shared
catalog, never from a list of ids kept here. Only agents that take a
per-launch model carry one (Claude and Codex).

Two readers, two disciplines: :func:`load_launch_presets` reads a saved file
and keeps every well-shaped row, so a model dropped from the catalog never
deletes the owner's preset on the next save; :func:`checked_launch_presets`
validates a Settings write and refuses anything the catalog does not offer.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

from src.launching.model_catalog import (
    CLAUDE_MODEL_SPECS,
    CODEX_MODEL_SPECS,
    available_values,
)

PRESET_AGENTS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "claude": CLAUDE_MODEL_SPECS,
    "codex": CODEX_MODEL_SPECS,
}

# A phone dropdown stays a short list; the cap also bounds a hand-edited file.
MAX_PRESETS = 12
MAX_NAME_LENGTH = 40


def efforts_for(agent: str, model: str) -> Tuple[str, ...]:
    """The efforts ``agent``'s ``model`` offers, ``()`` for an unknown pair."""
    specs = PRESET_AGENTS.get(agent, {})
    return tuple(specs.get(model, {}).get("efforts", ()))


def contexts_for(agent: str, model: str) -> Tuple[str, ...]:
    """The context tiers ``agent``'s ``model`` offers beyond its default window."""
    specs = PRESET_AGENTS.get(agent, {})
    return tuple(specs.get(model, {}).get("contexts", ()))


def load_launch_presets(raw: Dict[str, Any]) -> List[Dict[str, str]]:
    """The saved presets: every row with all five text fields, others dropped."""
    value = raw.get("launch_presets")
    if not isinstance(value, list):
        return []
    rows: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in ("id", "name", "agent", "model", "effort")}
        if all(isinstance(text, str) and text for text in row.values()):
            clean = {key: str(text) for key, text in row.items()}
            context = item.get("context")
            if isinstance(context, str) and context:
                clean["context"] = context
            rows.append(clean)
    return rows


def checked_launch_presets(value: Any) -> List[Dict[str, str]]:
    """``value`` as a clean preset list, or a ``ValueError`` naming the fault.

    A row without an ``id`` gets one, so the client adds a preset by sending
    its name, agent, model and effort alone.
    """
    if not isinstance(value, list):
        raise ValueError("launch_presets must be a list")
    if len(value) > MAX_PRESETS:
        raise ValueError(f"at most {MAX_PRESETS} launch presets")
    rows: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each launch preset must be an object")
        rows.append(_checked_preset(item))
    _require_unique(rows, "id", "launch preset ids must be unique")
    names = [row["name"].casefold() for row in rows]
    if len(set(names)) != len(names):
        raise ValueError("launch preset names must be unique")
    return rows


def _checked_preset(item: Dict[str, Any]) -> Dict[str, str]:
    name = " ".join(str(item.get("name") or "").split())
    if not name:
        raise ValueError("a launch preset needs a name")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"a launch preset name is at most {MAX_NAME_LENGTH} characters")
    agent = str(item.get("agent") or "")
    if agent not in PRESET_AGENTS:
        raise ValueError(f"{name}: {agent or 'no agent'!r} takes no per-launch model")
    model = str(item.get("model") or "")
    if model not in available_values(PRESET_AGENTS[agent]):
        raise ValueError(f"{name}: unsupported {agent} model {model!r}")
    effort = str(item.get("effort") or "")
    offered = efforts_for(agent, model)
    if effort not in offered:
        raise ValueError(
            f"{name}: effort {effort!r} is not offered by {model}; allowed: {', '.join(offered)}"
        )
    row = {
        "id": str(item.get("id") or "") or uuid.uuid4().hex[:8],
        "name": name,
        "agent": agent,
        "model": model,
        "effort": effort,
    }
    context = str(item.get("context") or "")
    if context:
        tiers = contexts_for(agent, model)
        if context not in tiers:
            allowed = ", ".join(tiers) if tiers else "none"
            raise ValueError(
                f"{name}: context {context!r} is not offered by {model}; allowed: {allowed}"
            )
        row["context"] = context
    return row


def _require_unique(rows: List[Dict[str, str]], key: str, message: str) -> None:
    values = [row[key] for row in rows]
    if len(set(values)) != len(values):
        raise ValueError(message)
