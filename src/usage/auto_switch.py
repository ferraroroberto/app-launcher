"""Whether a Sonnet-tier dispatch runs on Grok instead, to save Claude quota.

With Settings' ``grok_auto_switch`` on, a dispatch asking for the Sonnet
tier is launched as Grok Build when Claude's weekly use is within
:data:`SWITCH_MARGIN_POINTS` of the week's pace or above it. Pace is the
share of the week elapsed, the same figure the usage meter's pace tick
shows (``quotaPace`` in ``dom-utils.js``).

Every doubt keeps the lane on Claude: an Opus or Codex request, a Claude
reading that is not fresh, a week with no computable pace, Grok not
installed, or a repo Grok has not been trusted in. A lane that cannot be
switched says why, so the chief and the owner can see it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Within this many points of the pace line counts as near it (the owner's
# choice on the Board).
SWITCH_MARGIN_POINTS = 5
# The dispatch values that ask for the Sonnet tier; a bare tier is the
# pre-provider spelling old callers still send.
SONNET_DISPATCH_MODELS = ("claude:sonnet", "sonnet")
GROK_FALLBACK_MODEL = "grok:grok-4.5"
# Used when a weekly window carries no length of its own.
_WEEK_MINUTES = 10080


@dataclass(frozen=True)
class SwitchDecision:
    """The model to launch, and what happened to the switch.

    ``outcome`` is ``switched``, ``kept`` (the rule said stay on Claude) or
    ``skipped`` (the rule said switch, but something blocked it); ``reason``
    is a short machine-readable why. ``None`` outcome means the switch never
    applied (the toggle is off or the model is not the Sonnet tier).
    """

    model: str
    outcome: Optional[str] = None
    reason: str = ""
    weekly_pct: Optional[float] = None
    pace_pct: Optional[int] = None

    def payload(self, requested: str) -> Optional[Dict[str, Any]]:
        """The dispatch response's ``auto_switch`` block, ``None`` when it never applied."""
        if self.outcome is None:
            return None
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "requested": requested,
            "launched": self.model,
            "claude_weekly_pct": self.weekly_pct,
            "week_pace_pct": self.pace_pct,
        }


def _epoch_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return stamp.timestamp() if stamp.tzinfo else None
    return None


def week_pace(weekly: Optional[Dict[str, Any]], now: float) -> Optional[int]:
    """Percent of the weekly window elapsed at ``now``; ``None`` when unknown.

    Mirrors the meter's ``quotaPace``: a reset already in the past gives no
    pace, since the window it described is over.
    """
    if not isinstance(weekly, dict):
        return None
    reset = _epoch_seconds(weekly.get("resets_at"))
    if reset is None or now > reset:
        return None
    minutes = weekly.get("duration_minutes")
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        minutes = _WEEK_MINUTES
    length = float(minutes) * 60.0
    elapsed = (now - (reset - length)) / length
    return round(min(1.0, max(0.0, elapsed)) * 100)


def grok_trusts(folder: Path, grok_home: Path) -> Optional[bool]:
    """Whether Grok's folder-trust store trusts exactly ``folder``.

    ``None`` when the store cannot be read. Only an exact entry counts: Grok
    treats a nested git checkout under a trusted folder as its own workspace,
    so a trusted parent folder does not cover a repo.
    """
    store = grok_home / "trusted_folders.toml"
    if not store.exists():
        return False
    try:
        data = tomllib.loads(store.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    folders = data.get("folders")
    if not isinstance(folders, dict):
        return False
    wanted = os.path.normcase(os.path.normpath(str(folder)))
    for path, entry in folders.items():
        if os.path.normcase(os.path.normpath(str(path))) == wanted:
            return isinstance(entry, dict) and entry.get("trusted") is True
    return False


def decide(
    model: str,
    *,
    enabled: bool,
    claude_line: Optional[Dict[str, Any]],
    now: float,
    grok_installed: bool,
    repo_trusted: Optional[bool],
) -> SwitchDecision:
    """The model a dispatch should launch, per the auto-switch rule."""
    if not enabled or model not in SONNET_DISPATCH_MODELS:
        return SwitchDecision(model)
    line = claude_line if isinstance(claude_line, dict) else {}
    if line.get("state") != "available" or line.get("stale"):
        return SwitchDecision(model, "kept", "claude_reading_not_fresh")
    weekly = line.get("weekly")
    pct = weekly.get("used_percentage") if isinstance(weekly, dict) else None
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return SwitchDecision(model, "kept", "claude_weekly_unknown")
    pace = week_pace(weekly, now)
    if pace is None:
        return SwitchDecision(model, "kept", "week_pace_unknown", weekly_pct=pct)
    if pct < pace - SWITCH_MARGIN_POINTS:
        return SwitchDecision(model, "kept", "claude_under_pace", weekly_pct=pct, pace_pct=pace)
    if not grok_installed:
        return SwitchDecision(model, "skipped", "grok_not_installed", weekly_pct=pct, pace_pct=pace)
    if repo_trusted is None:
        return SwitchDecision(model, "skipped", "grok_trust_unreadable", weekly_pct=pct, pace_pct=pace)
    if not repo_trusted:
        return SwitchDecision(model, "skipped", "repo_not_trusted_by_grok", weekly_pct=pct, pace_pct=pace)
    return SwitchDecision(
        GROK_FALLBACK_MODEL, "switched", "claude_near_pace", weekly_pct=pct, pace_pct=pace
    )
