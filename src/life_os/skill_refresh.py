"""Tell a resumed Life OS conversation to re-read skill instructions that changed.

A native resume reopens the session exactly as it was, so the agent keeps the
skill instructions it loaded when the conversation began, and its own earlier
turns reinforce them. When the skill's instructions changed since, the resume
carries one short prompt asking the agent to re-read them first.

**Fingerprint.** A sha256 over the skill's instruction set: every ``.md`` file
in the skill folder outside ``conversations/``, ``memory/`` and ``context/``
(``SKILL.md``, ``description.md``, ``maintenance.md``, ``assets/*.md``...),
plus each shared guide under ``.claude/skills/_shared/`` that ``SKILL.md``
names. Content, never mtimes, so a checkout that only touches timestamps is not
a change. Only instruction files are read; a capture body never is.

**Decision**, per conversation (keyed by skill and native session id):

- a recorded fingerprint: ``changed`` when it differs from today's, else
  ``unchanged``;
- none recorded (a conversation older than this feature, or never resumed):
  ``changed`` when git has a commit to those files at or after the capture's
  creation time (read from its ``YYYY-MM-DD-HHMM-`` file name), else
  ``unchanged``;
- the fingerprint, or with no record git, can't be read: ``unknown``, which
  resumes without the prompt.

``off`` is the Settings toggle turned off: nothing is computed or recorded.

Store: ``webapp/life-os-skill-hashes.json``, gitignored, atomically replaced:
``{"<skill>:<sid>": "<sha256>"}``, the newest :data:`MAX_RECORDS` kept. A
missing or corrupt file reads as nothing recorded, which falls back to git.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src._json_io import atomic_write_json
from src.git_utils import run_git
from src.scanner import skills_dir_for
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
SKILL_HASHES_FILE = PROJECT_ROOT / "webapp" / "life-os-skill-hashes.json"

# One entry per resumed conversation; old ones only cost a git fallback.
MAX_RECORDS = 1000

# Private or per-conversation content, never instructions.
EXCLUDED_DIRS = frozenset({"conversations", "memory", "context"})

_SHARED_DIR = "_shared"
_SHARED_REF = re.compile(r"_shared/([A-Za-z0-9_.-]+\.md)")
# The id goes into a double-quoted command-line argument.
_SAFE_SKILL_ID = re.compile(r"[A-Za-z0-9_.-]+")
_CAPTURE_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})(?:-(\d{4}))?")

_lock = threading.Lock()


@dataclass(frozen=True)
class RefreshPlan:
    """What a resume does about the skill's instructions.

    ``suffix`` is appended to the resume flags verbatim: ``""`` unless the
    state is ``changed``, so every other state launches exactly as before.
    """

    state: str
    key: str = ""
    fingerprint: Optional[str] = None
    suffix: str = ""


def refresh_prompt(skill_id: str) -> str:
    return (f"This skill's instructions changed since this conversation began: re-read "
            f".claude/skills/{skill_id}/SKILL.md (and the files it points to) and follow "
            "the current version over anything earlier in this conversation.")


def instruction_files(skills_dir: Path, skill_id: str) -> Optional[List[Path]]:
    """The files the fingerprint covers, sorted, or ``None`` without a readable ``SKILL.md``."""
    skill_dir = skills_dir / skill_id
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    files: List[Path] = []
    for folder, dirs, names in os.walk(skill_dir):
        if Path(folder) == skill_dir:
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        files.extend(Path(folder) / n for n in names if n.endswith(".md"))
    shared = skills_dir / _SHARED_DIR
    files.extend(p for p in (shared / n for n in set(_SHARED_REF.findall(text))) if p.is_file())
    # A string sort: Windows compares paths case-insensitively, and the hash
    # must not depend on the platform's order.
    return sorted(set(files), key=lambda p: p.as_posix())


def skill_fingerprint(skills_dir: Path, files: List[Path]) -> Optional[str]:
    """sha256 over each file's path (relative to ``skills_dir``) and content; ``None`` if one can't be read."""
    digest = hashlib.sha256()
    for path in files:
        try:
            content = path.read_bytes()
        except OSError:
            return None
        digest.update(path.relative_to(skills_dir).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def capture_created(capture_name: str) -> Optional[datetime]:
    """A capture's local creation time from its file name; the day's start without ``HHMM``."""
    match = _CAPTURE_STAMP.match(capture_name)
    if not match:
        return None
    day, hhmm = match.groups()
    try:
        return datetime.strptime(f"{day}-{hhmm or '0000'}", "%Y-%m-%d-%H%M")
    except ValueError:
        return None


def committed_since(repo: Path, files: List[Path], since: datetime) -> Optional[bool]:
    """Whether git has a commit to ``files`` at or after ``since``; ``None`` when git can't say."""
    try:
        rels = [p.relative_to(repo).as_posix() for p in files]
    except ValueError:
        return None
    out = run_git(repo, ["log", "-1", "--format=%ct", "--", *rels])
    if out is None:
        return None
    if not out:
        return False
    try:
        return datetime.fromtimestamp(int(out)) >= since
    except ValueError:
        return None


def read_records(path: Optional[Path] = None) -> Dict[str, str]:
    """The recorded fingerprints; ``{}`` when nothing (readable) is on file."""
    target = Path(path or SKILL_HASHES_FILE)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ Life OS skill fingerprints unreadable: %s", exc.__class__.__name__)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def plan_refresh(
    cfg: WebappConfig, skill_id: str, sid: str, capture_name: str,
    path: Optional[Path] = None,
) -> RefreshPlan:
    """Decide whether this resume asks the agent to re-read its skill, and log the decision."""
    if not cfg.life_os_refresh_skill_instructions:
        return RefreshPlan("off")
    key = f"{skill_id}:{sid.lower()}"
    root = Path(cfg.life_os_dir)
    skills_dir = skills_dir_for(root)
    files = instruction_files(skills_dir, skill_id) if _SAFE_SKILL_ID.fullmatch(skill_id) else None
    fingerprint = skill_fingerprint(skills_dir, files) if files else None
    if fingerprint is None:
        logger.info("ℹ️ Life OS resume skill=%s instructions=unknown "
                    "(fingerprint unreadable), resuming without the re-read prompt", skill_id)
        return RefreshPlan("unknown", key)
    recorded = read_records(path).get(key)
    if recorded is not None:
        changed: Optional[bool] = recorded != fingerprint
    else:
        created = capture_created(capture_name)
        changed = committed_since(root, files, created) if created else None
    if changed is None:
        logger.info("ℹ️ Life OS resume skill=%s instructions=unknown "
                    "(no recorded fingerprint, git history unreadable), resuming without "
                    "the re-read prompt", skill_id)
        return RefreshPlan("unknown", key, fingerprint)
    state = "changed" if changed else "unchanged"
    logger.info("ℹ️ Life OS resume skill=%s instructions=%s", skill_id, state)
    suffix = f' "{refresh_prompt(skill_id)}"' if changed else ""
    return RefreshPlan(state, key, fingerprint, suffix)


def record_refresh(plan: RefreshPlan, path: Optional[Path] = None) -> None:
    """Record the fingerprint this resume left the conversation on.

    Called after the session launched. An ``unknown`` plan with a fingerprint
    (git could not say) is recorded too, so a later change is still caught. A
    failed write is logged, never raised: the session is already running.
    """
    if not plan.key or plan.fingerprint is None:
        return
    target = Path(path or SKILL_HASHES_FILE)
    with _lock:
        current = read_records(target)
        if current.get(plan.key) == plan.fingerprint:
            return
        current.pop(plan.key, None)
        current[plan.key] = plan.fingerprint
        kept = dict(list(current.items())[-MAX_RECORDS:])
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(target, kept)
        except OSError as exc:
            logger.warning("⚠️ Life OS skill fingerprint not recorded: %s", exc.__class__.__name__)
