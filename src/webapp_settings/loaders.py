"""How each :class:`src.webapp_config.WebappConfig` field is read from the parsed
JSON: the generic per-field cast, and a custom loader for every field whose
on-disk shape its annotation alone cannot describe.
"""

from __future__ import annotations

from dataclasses import MISSING
from typing import Any, Callable, Dict

from src import request_health
from src.launching.model_catalog import (
    CODEX_MODEL_SPECS,
    load_hidden_models,
)
from src.launching.presets import load_launch_presets
from src.usage.providers import load_usage_providers
from src.webapp_settings.values import (
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_MODEL_EFFORTS,
    DEFAULT_PI_MODEL,
    VALID_CODEX_MODELS,
    VALID_PI_EFFORTS,
    VALID_PI_MODELS,
    valid_claude_efforts_for,
)


def _load_secrets(raw: dict) -> Dict[str, str]:
    """``secrets``, honouring the legacy ``webhook_secrets`` key.

    The block holds every ``Job.env`` value, but it began as webhook secrets
    only, and old config files in the wild still spell it that way.
    """
    return {
        str(key): str(value)
        for key, value in (
            raw.get("secrets") or raw.get("webhook_secrets") or {}
        ).items()
    }


def _load_api_tokens(raw: dict) -> list:
    """``api_tokens`` — a list of *records*, not of strings, and a malformed
    entry is dropped rather than stringified: a half-written token record can
    only ever fail closed."""
    return [
        dict(token)
        for token in (raw.get("api_tokens") or [])
        if isinstance(token, dict)
    ]


def _load_documents(raw: dict) -> list:
    """``documents`` — a list of *records*; a non-object entry is dropped here
    and ``src.documents`` checks each record's fields when it is used."""
    return [
        dict(entry)
        for entry in (raw.get("documents") or [])
        if isinstance(entry, dict)
    ]


def _load_pi_model(raw: dict) -> str:
    """Migrate the three retired pinned ids to their current successors."""
    value = str(raw.get("pi_model") or DEFAULT_PI_MODEL)
    return {
        "claude-opus-4-8": "claude-opus-5",
        "claude-sonnet-4-6": "claude-sonnet-5",
        "gpt-5.5": "gpt-5.6-sol",
    }.get(value, value)


def _load_codex_model_efforts(raw: dict) -> Dict[str, str]:
    """Load only supported model/effort pairs and seed newly added models.

    ``codex_effort`` mirrors the selected model's entry, and a hand-edited file
    can make them disagree. The selected model's own ``codex_effort`` has always
    been the one launched, so it wins here, and ``effective_effort`` reads the
    dict alone.
    """
    result = dict(DEFAULT_CODEX_MODEL_EFFORTS)
    authored = raw.get("codex_model_efforts") or {}
    if isinstance(authored, dict):
        for model, effort in authored.items():
            if (
                model in VALID_CODEX_MODELS
                and effort in CODEX_MODEL_SPECS[model]["efforts"]
            ):
                result[str(model)] = str(effort)
    selected = raw.get("codex_model") or DEFAULT_CODEX_MODEL
    mirrored = _load_codex_effort(raw)
    if (
        ("codex_model" in raw or "codex_model_efforts" in raw)
        and selected in VALID_CODEX_MODELS
        and mirrored in CODEX_MODEL_SPECS[selected]["efforts"]
    ):
        result[str(selected)] = mirrored
    return result


def _load_claude_model_efforts(raw: dict) -> Dict[str, str]:
    """Load the Claude overrides: only a known model with an effort it supports."""
    authored = raw.get("claude_model_efforts") or {}
    if not isinstance(authored, dict):
        return {}
    return {
        str(model): str(effort)
        for model, effort in authored.items()
        if effort in valid_claude_efforts_for(str(model))
    }


def _load_pi_model_efforts(raw: dict) -> Dict[str, str]:
    """Load the Pi overrides: only a known model with a ``--thinking`` level."""
    authored = raw.get("pi_model_efforts") or {}
    if not isinstance(authored, dict):
        return {}
    return {
        str(model): str(effort)
        for model, effort in authored.items()
        if str(model) in VALID_PI_MODELS and effort in VALID_PI_EFFORTS
    }


def _load_codex_effort(raw: dict) -> str:
    """Do not reinterpret the old global High default as Luna's preference."""
    if "codex_model" not in raw and "codex_model_efforts" not in raw:
        return DEFAULT_CODEX_EFFORT
    return str(raw.get("codex_effort") or DEFAULT_CODEX_EFFORT)


#: Fields whose on-disk shape can't be derived from the annotation alone.
#: Everything else is handled by the generic loop in
#: :func:`src.webapp_config.load_webapp_config`, so a new setting is declared
#: once — on :class:`~src.webapp_config.WebappConfig` — and both directions
#: follow it.
_CUSTOM_LOADERS: Dict[str, Callable[[dict], Any]] = {
    "secrets": _load_secrets,
    "api_tokens": _load_api_tokens,
    "documents": _load_documents,
    "pi_model": _load_pi_model,
    "codex_effort": _load_codex_effort,
    "codex_model_efforts": _load_codex_model_efforts,
    "claude_model_efforts": _load_claude_model_efforts,
    "pi_model_efforts": _load_pi_model_efforts,
    "usage_providers": load_usage_providers,
    "hidden_models": load_hidden_models,
    "request_budgets": request_health.load_request_budgets,
    "launch_presets": load_launch_presets,
}


def _coerce(value: Any, annotation: Any, default: Any) -> Any:
    """Cast one raw JSON value to the type its dataclass field declares.

    ``from __future__ import annotations`` is active, so a field's ``type``
    arrives as the *string* ``"str"`` / ``"int"`` / ``"bool"`` / ``"list"``;
    both spellings are accepted so this keeps working if that import ever
    goes away. An unusable number falls back to the declared default rather
    than raising — a hand-edited config with a null in an int field
    shouldn't take the whole webapp down. Unknown annotations pass through
    untouched; those are the ones that belong in ``_CUSTOM_LOADERS``.
    """
    if annotation in (str, "str"):
        return str(value)
    if annotation in (bool, "bool"):
        # A hand-edited config's non-boolean value (e.g. the string
        # ``"false"``, truthy under `bool()`) or an unusable null (were it
        # ever to reach here) falls back to the declared default rather
        # than being coerced by truthiness — matching the int
        # branch's discipline just below.
        return value if isinstance(value, bool) else default
    if annotation in (int, "int"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if annotation in (list, "list"):
        return [str(item) for item in value]
    return value


def load_field(spec, raw: dict) -> Any:
    """Read one :class:`~src.webapp_config.WebappConfig` field out of the parsed JSON.

    Two default disciplines, both inherited from the per-field block this
    replaced and both derivable from the field itself:

    - A ``default_factory`` field (every path-ish setting, plus the list and
      dict ones) treats *any* falsy on-disk value as absent and re-derives
      the default — an empty ``projects_dir`` means "you never set one", not
      "scan the filesystem root".
    - A literal-default field keeps a falsy value as authored, because that
      is how features here are switched off: an empty ``voice_transcriber_url``
      hides the 🎤 button, an empty ``auth_token`` disables the bearer gate.

    Collapsing the two would silently re-enable whatever the user turned off,
    so the distinction is load-bearing rather than incidental. An explicit
    JSON ``null`` is the one falsy value treated as absent everywhere: it is
    never a meaningful "off" for any setting here (``""`` and ``0`` are), and
    the old per-field block turned it into ``"None"`` or a ``TypeError``
    depending on the field.
    """
    custom = _CUSTOM_LOADERS.get(spec.name)
    if custom is not None:
        return custom(raw)
    if spec.default_factory is not MISSING:  # type: ignore[misc]
        default = spec.default_factory()
        value = raw.get(spec.name)
        return _coerce(value, spec.type, default) if value else default
    default = spec.default
    value = raw.get(spec.name, default)
    if value is None:
        return default
    return _coerce(value, spec.type, default)
