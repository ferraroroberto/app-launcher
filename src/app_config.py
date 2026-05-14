"""Application-level configuration loader.

Source of truth for cross-surface settings (log level, webapp embed
section consumed by the tray's webapp manager). Webapp UI preferences
live in `src/webapp_config.py`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass
class AppConfig:
    log_level: str = "INFO"
    # Optional webapp section — when missing, the tray spawns the webapp
    # on `:8445` with default settings. Set webapp.enabled:false to opt out.
    webapp: Dict = field(default_factory=dict)


def load_app_config(path: Optional[Path] = None) -> AppConfig:
    """Load `config/config.json` from next to this file (or an override)."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "config.json"
    else:
        path = Path(path).resolve()

    if not path.exists():
        logger.info(f"📂 Config not found at {path}, using defaults")
        return AppConfig()

    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate(raw)
    return AppConfig(
        log_level=raw.get("log_level", "INFO"),
        webapp=raw.get("webapp") or {},
    )


def _validate(raw: Dict) -> None:
    if "log_level" in raw and raw["log_level"] not in VALID_LOG_LEVELS:
        raise ValueError(f"log_level must be one of {VALID_LOG_LEVELS}")
