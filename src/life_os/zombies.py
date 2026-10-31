"""Finding and cleaning up Life OS "zombie" conversations (#1738).

A zombie is a skill session opened by mistake: it loaded the skill's context and
posted its greeting, and the capture pipeline recorded it as a real
conversation. In the *capture file* the launcher's skill-load prompt is already
gone (fleet-config's ``render_markdown`` drops it as a preamble), so the rule is
expressed on the rendered body: **no ``**You**:`` block at all**.

That preamble filter is a loose regex, so a real first message that looks like a
heading would also be dropped and its conversation would look like a zombie.
:func:`classify` therefore has a second, independent bound (a greeting is one or
two short assistant blocks) and treats anything it can't establish as skipped,
never as a zombie. Deletion goes through :func:`src.life_os.delete.delete_conversation`,
the same routine as the Delete endpoint.

Pure classification first, then :func:`run_sweep`, the effectful walk. Nothing
personal from a capture body reaches a log or the summary: logs carry skill id
and file name, the Telegram summary carries counts only.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.clients import session as session_client
from src.life_os.capture import parse_capture
from src.life_os.delete import ConversationDeleteError, delete_conversation, live_conversations
from src.life_os.history import read_captures
from src.life_os.links import is_link
from src.life_os.zombie_backup import Stash, backup_root, purge_old_backups
from src.scanner import Skill, scan_skills, skills_dir_for
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

MODE_BACKUP = "backup"
MODE_DELETE = "delete"
MODE_DRY_RUN = "dry-run"
MODES = (MODE_BACKUP, MODE_DELETE, MODE_DRY_RUN)

DEFAULT_GRACE_HOURS = 2.0
DEFAULT_MAX_PER_RUN = 25
DEFAULT_RETENTION_DAYS = 30
# A greeting is one or two blocks; a long conversation whose user turns the
# capture pipeline swallowed is not (see the module docstring).
MAX_ASSISTANT_BLOCKS = 2
MAX_BODY_BYTES = 16 * 1024
# A capture dated this far ahead of now means the clock is suspect.
_CLOCK_SKEW_S = 120
_READ_CHUNK = 40

VERDICT_ZOMBIE = "zombie"
VERDICT_KEPT = "kept"
VERDICT_SKIPPED = "skipped"

KEPT_LIVE = "live"
KEPT_GRACE = "grace"
KEPT_CLOCK = "clock"
KEPT_USER_TURN = "user_turn"
KEPT_SUBSTANTIAL = "substantial"
SKIPPED_UNREADABLE = "unreadable"
SKIPPED_NO_IDENTITY = "no_identity"
SKIPPED_OVERSIZED = "oversized"
SKIPPED_UNPARSED = "unparsed"
SKIPPED_CHANGED = "changed"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BREAKER = 3
EXIT_UNCONFIRMED = 4


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str = ""


def classify(
    source: Mapping[str, Any], *, mtime: Optional[float], now: float, live: Mapping[str, Any],
    grace_s: float, max_assistant_blocks: int = MAX_ASSISTANT_BLOCKS,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> Verdict:
    """Classify one capture from its :func:`read_captures` result; first match wins.

    Every check is fail-safe: anything that can't be established is
    ``skipped``, never ``zombie``. ``live`` is the live-conversation map
    (lower-case session id keys); the caller has already established it.
    """
    if mtime is None or "revision" not in source:
        return Verdict(VERDICT_SKIPPED, SKIPPED_UNREADABLE)
    if not source.get("native") or not source.get("sid"):
        return Verdict(VERDICT_SKIPPED, SKIPPED_NO_IDENTITY)
    if source.get("oversized") or "body" not in source:
        return Verdict(VERDICT_SKIPPED, SKIPPED_OVERSIZED)
    if str(source["sid"]).lower() in live:
        return Verdict(VERDICT_KEPT, KEPT_LIVE)
    if mtime > now + _CLOCK_SKEW_S:
        return Verdict(VERDICT_KEPT, KEPT_CLOCK)
    if now - mtime < grace_s:
        return Verdict(VERDICT_KEPT, KEPT_GRACE)
    body = str(source["body"])
    entries = parse_capture(body)["entries"]
    if not entries:
        return Verdict(VERDICT_SKIPPED, SKIPPED_UNPARSED)
    if any(entry["kind"] == "user" for entry in entries):
        return Verdict(VERDICT_KEPT, KEPT_USER_TURN)
    if len(entries) > max_assistant_blocks or len(body.encode("utf-8")) > max_body_bytes:
        return Verdict(VERDICT_KEPT, KEPT_SUBSTANTIAL)
    return Verdict(VERDICT_ZOMBIE)


@dataclass
class SweepResult:
    mode: str
    total: int = 0
    zombies: int = 0
    removed: int = 0
    failed: int = 0
    purged: int = 0
    kept: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    unconfirmed: bool = False
    tripped: bool = False
    max_per_run: int = DEFAULT_MAX_PER_RUN

    @property
    def exit_code(self) -> int:
        if self.unconfirmed:
            return EXIT_UNCONFIRMED
        if self.tripped:
            return EXIT_BREAKER
        return EXIT_FAILED if self.failed else EXIT_OK


@dataclass(frozen=True)
class _Candidate:
    skill: Skill
    path: Path
    revision: str


def _skill_captures(root: Path, skill: Skill) -> List[Path]:
    """The flat ``.md`` captures in one skill's ``conversations/`` folder."""
    folder = skills_dir_for(root) / skill.id / "conversations"
    try:
        if not folder.is_dir() or is_link(folder):
            return []
        children = sorted(folder.glob("*.md"))
    except OSError:
        return []
    return [p for p in children if p.name != "index.md" and not is_link(p) and p.is_file()]


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def run_sweep(
    cfg: WebappConfig, *, mode: str = MODE_BACKUP, now: Optional[float] = None,
    grace_hours: float = DEFAULT_GRACE_HOURS, max_per_run: int = DEFAULT_MAX_PER_RUN,
    retention_days: int = DEFAULT_RETENTION_DAYS, backup_dir: Optional[Path] = None,
) -> SweepResult:
    """Classify every Life OS capture and, unless ``mode`` is dry-run, act on the zombies.

    Stands down (``unconfirmed``) when the session host can't say what is live;
    trips the breaker (acts on none) when more than ``max_per_run`` zombies are
    found, which is what a change in fleet-config's capture parser would look
    like. A capture that changed between classify and act is dropped. One
    capture failing is logged and counted and does not stop the rest.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    result = SweepResult(mode=mode, max_per_run=max_per_run)
    backup_dir = backup_dir or backup_root()
    clock = time.time() if now is None else now
    try:
        _sweep(cfg, result, clock, grace_hours * 3600.0, max_per_run, backup_dir)
    finally:
        if mode != MODE_DRY_RUN:
            result.purged = purge_old_backups(backup_dir, retention_days)
    return result


def _sweep(
    cfg: WebappConfig, result: SweepResult, now: float, grace_s: float,
    max_per_run: int, backup_dir: Path,
) -> None:
    try:
        live = live_conversations(cfg)
    except session_client.SessionHostError as exc:
        logger.warning("⚠️ Zombie cleanup: session host unavailable (%s); nothing was touched", exc)
        result.unconfirmed = True
        return
    root, fleet_dir = Path(cfg.life_os_dir), Path(cfg.fleet_config_dir)
    candidates: List[_Candidate] = []
    for skill in scan_skills(root):
        paths = _skill_captures(root, skill)
        for start in range(0, len(paths), _READ_CHUNK):
            chunk = paths[start:start + _READ_CHUNK]
            for path, source in zip(chunk, read_captures(fleet_dir, list(chunk))):
                result.total += 1
                verdict = classify(source, mtime=_mtime(path), now=now, live=live, grace_s=grace_s)
                if verdict.kind == VERDICT_ZOMBIE:
                    result.zombies += 1
                    candidates.append(_Candidate(skill, path, str(source["revision"])))
                elif verdict.kind == VERDICT_KEPT:
                    result.kept[verdict.reason] += 1
                else:
                    result.skipped[verdict.reason] += 1
    if len(candidates) > max_per_run:
        logger.error("❌ Zombie cleanup: breaker tripped, %d candidates over the limit of %d; "
                     "nothing was touched", len(candidates), max_per_run)
        result.tripped = True
        return
    if result.mode == MODE_DRY_RUN:
        return
    today = date.fromtimestamp(now)
    for candidate in candidates:
        if not _act(cfg, result, candidate, now, grace_s, fleet_dir,
                    Stash.for_run(backup_dir, today, candidate.skill.id) if result.mode == MODE_BACKUP else None):
            break


def _act(
    cfg: WebappConfig, result: SweepResult, candidate: _Candidate, now: float,
    grace_s: float, fleet_dir: Path, stash: Optional[Stash],
) -> bool:
    """Re-verify one zombie and remove it; ``False`` stops the sweep (host unknown)."""
    skill_id, name = candidate.skill.id, candidate.path.name
    try:
        live = live_conversations(cfg)
    except session_client.SessionHostError as exc:
        logger.warning("⚠️ Zombie cleanup: session host unavailable mid-run (%s); stopping", exc)
        result.unconfirmed = True
        return False
    source = read_captures(fleet_dir, [candidate.path])[0]
    verdict = classify(source, mtime=_mtime(candidate.path), now=now, live=live, grace_s=grace_s)
    if verdict.kind != VERDICT_ZOMBIE or source.get("revision") != candidate.revision:
        result.skipped[SKIPPED_CHANGED] += 1
        logger.info("ℹ️ Zombie cleanup: dropped, changed since it was classified: skill=%s file=%s",
                    skill_id, name)
        return True
    try:
        delete_conversation(cfg, skill_id, candidate.path, source, stash=stash)
    except (ConversationDeleteError, OSError, subprocess.SubprocessError, ValueError) as exc:
        result.failed += 1
        logger.warning("⚠️ Zombie cleanup: could not remove skill=%s file=%s: %s", skill_id, name, exc)
        return True
    result.removed += 1
    logger.info("ℹ️ Zombie cleanup: %s skill=%s file=%s",
                "backed up" if stash is not None else "deleted", skill_id, name)
    return True


def summary_text(result: SweepResult) -> tuple[str, str]:
    """``(title, body)`` for the one Telegram message per run. Counts only, never a name."""
    title = f"Zombie cleanup (mode={result.mode})"
    if result.unconfirmed:
        return f"{title}: not confirmed", ("The session host could not be asked, so nothing was "
                                           f"touched. {result.removed} removed before it stopped.")
    if result.tripped:
        return f"{title}: breaker tripped", (f"{result.zombies} candidates is over the limit of "
                                              f"{result.max_per_run}; nothing was touched.")
    live_grace = result.kept[KEPT_LIVE] + result.kept[KEPT_GRACE]
    parts = [f"{result.removed} removed", f"{live_grace} kept (live/grace)",
             f"{sum(result.skipped.values())} skipped"]
    if result.failed:
        parts.append(f"{result.failed} failed")
    if result.purged:
        parts.append(f"{result.purged} old backup day(s) purged")
    return title, ", ".join(parts)


def counts_lines(result: SweepResult) -> List[Dict[str, Any]]:
    """The numbers a dry run prints, as ``{"label", "count"}`` rows: no names, no content."""
    rows: List[Dict[str, Any]] = [
        {"label": "captures", "count": result.total},
        {"label": "zombies", "count": result.zombies},
    ]
    rows += [{"label": f"kept: {k}", "count": result.kept[k]}
             for k in (KEPT_LIVE, KEPT_GRACE, KEPT_CLOCK, KEPT_USER_TURN, KEPT_SUBSTANTIAL)]
    rows += [{"label": f"skipped: {k}", "count": result.skipped[k]}
             for k in (SKIPPED_UNREADABLE, SKIPPED_NO_IDENTITY, SKIPPED_OVERSIZED,
                       SKIPPED_UNPARSED, SKIPPED_CHANGED)]
    return rows
