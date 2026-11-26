"""Legacy ``config/webapp_config.json`` keys carried to their current shape, in
memory, before :func:`src.webapp_config.load_webapp_config` reads the fields.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from src.webapp_settings.values import (
    DEFAULT_CLAUDE_EFFORT,
    DEFAULT_PI_EFFORT,
    VALID_CLAUDE_MODELS,
    VALID_PI_EFFORTS,
    VALID_PI_MODELS,
    valid_claude_efforts_for,
)

logger = logging.getLogger(__name__)

# Keys renamed since a live ``config/webapp_config.json`` may have been written
# (the file is gitignored, so a pull never rewrites it): old name -> new name.
# ``claude_config_dir`` held the fleet-config checkout root all along, not
# Claude's own config dir.
_LEGACY_KEYS = {"claude_config_dir": "fleet_config_dir"}


def migrate_legacy_keys(raw: Dict[str, Any], source: Path) -> Dict[str, Any]:
    """``raw`` with each renamed key carried over to its new name.

    In memory only: the next save writes the new name and drops the old one, so
    the live file migrates itself the first time a setting changes. A new key
    already present wins over the old one.
    """
    migrated = dict(raw)
    for old, new in _LEGACY_KEYS.items():
        if old not in migrated:
            continue
        legacy = migrated.pop(old)
        if new not in migrated:
            migrated[new] = legacy
            logger.info(
                f"ℹ️  {source.name}: key '{old}' is now '{new}'; read it under "
                f"the new name (the file is rewritten on the next save)"
            )
    migrated = _migrate_global_effort(
        migrated, source, legacy_key="claude_effort", dict_key="claude_model_efforts",
        default=DEFAULT_CLAUDE_EFFORT, models=VALID_CLAUDE_MODELS,
        allowed_for=valid_claude_efforts_for,
    )
    return _migrate_global_effort(
        migrated, source, legacy_key="pi_effort", dict_key="pi_model_efforts",
        default=DEFAULT_PI_EFFORT, models=VALID_PI_MODELS,
        allowed_for=lambda model: VALID_PI_EFFORTS,
    )


def _migrate_global_effort(
    raw: Dict[str, Any],
    source: Path,
    *,
    legacy_key: str,
    dict_key: str,
    default: str,
    models: Tuple[str, ...],
    allowed_for: Callable[[str], Tuple[str, ...]],
) -> Dict[str, Any]:
    """Turn a legacy global effort key into per-model overrides.

    One global value was sent to every model of the agent, so ``default`` (the
    old default, ``high``) cannot be told from "never chose" and is dropped:
    copying it to every model would keep Opus one level above its own default,
    which is the bug. Any other value was a deliberate choice and is carried to
    every model that supports it, so nothing changes silently (the live Pi
    ``medium`` stays ``medium``). An existing ``dict_key`` wins. In memory only,
    like the key renames above: the next save writes the new shape and drops the
    old key.
    """
    if legacy_key not in raw:
        return raw
    migrated = dict(raw)
    legacy = migrated.pop(legacy_key)
    if dict_key in migrated:
        return migrated
    carried = {model: legacy for model in models if legacy in allowed_for(model)}
    if legacy in (None, "", default) or not carried:
        logger.info(
            f"ℹ️  {source.name}: legacy {legacy_key} {legacy!r} dropped; every "
            f"model runs at its own default effort"
        )
        return migrated
    migrated[dict_key] = carried
    logger.info(
        f"ℹ️  {source.name}: legacy {legacy_key} {legacy!r} carried to every "
        f"model as a per-model override"
    )
    return migrated
