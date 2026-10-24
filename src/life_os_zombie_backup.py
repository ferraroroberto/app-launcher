"""Backup, restore and retention for the Life OS zombie cleanup (#1738).

``backup`` mode moves what a Delete would remove into a dated folder under the
launcher's runtime data (outside the Life OS tree, so the capture pipeline never
sees it)::

    life-os-zombie-backup/
      2026-10-20/                  one folder per run date (local)
        <skill-id>/
          <capture>.md
          <sid>.jsonl              native transcript, when one existed
          <sid>/                   its sidecar folder, when one existed
          <capture>.md.manifest.json

The manifest holds everything a restore needs, saved verbatim at backup time:
nothing is derived later. :func:`restore` refuses to overwrite anything that has
appeared since, and leaves the backup untouched when it refuses.

Blocking filesystem work: callers run it off the event loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._json_io import atomic_write_json
from src.life_os_index import resync_search_index, restore_index_entry
from src.life_os_title import run_title_cli
from src.life_os_transcript import SESSION_ID_RE, claude_projects_dir
from src.runtime_data import runtime_data_dir
from src.scanner import skills_dir_for
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "life-os-zombie-backup"
MANIFEST_SUFFIX = ".manifest.json"
MANIFEST_VERSION = 1
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_TITLE_CHARS = 120
# A zombie capture is a greeting, so its first 16 KiB is all of it; the cap
# only bounds a stray large file a query would otherwise read whole.
_SEARCH_HEAD_BYTES = 16 * 1024
# 30 days of nightly runs capped at 25 each tops out near 750; a phone list
# past this is unreadable anyway, and a query narrows it.
MAX_ARCHIVED_ROWS = 200

STATUS_RESTORED = "restored"
STATUS_REFUSED = "refused"
STATUS_FAILED = "failed"


def backup_root() -> Path:
    """``<runtime data>/app-launcher/life-os-zombie-backup``, resolved per call."""
    return runtime_data_dir("app-launcher") / BACKUP_DIRNAME


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(path))


@dataclass(frozen=True)
class Stash:
    """Where one skill's captures go for one run: ``<root>/<date>/<skill>``."""

    folder: Path

    @classmethod
    def for_run(cls, root: Path, day: date, skill_id: str) -> "Stash":
        return cls(root / day.isoformat() / skill_id)

    def manifest_path(self, capture: str) -> Path:
        return self.folder / f"{capture}{MANIFEST_SUFFIX}"

    def write_manifest(self, capture: str, data: Dict[str, Any]) -> None:
        """Write the manifest, keeping any earlier non-empty value a retry would lose.

        A retry after a half-finished backup finds the transcript already moved,
        so its own lookup reports no project; the first attempt's value stays.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.manifest_path(capture)
        merged = dict(data)
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if isinstance(previous, dict):
            for key, value in previous.items():
                if value and not merged.get(key):
                    merged[key] = value
        atomic_write_json(path, merged)


def build_manifest(
    skill_id: str, capture: str, source: Dict[str, Any], entry: Dict[str, Any],
    *, project: str = "", files: int = 0, folders: int = 0,
) -> Dict[str, Any]:
    row = entry.get("index_json_row")
    return {
        "version": MANIFEST_VERSION,
        "skill": skill_id,
        "capture": capture,
        "agent": str(source.get("agent", "")),
        "sid": str(source.get("sid", "")).lower(),
        "moved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "transcript_project": project,
        "transcript_files": files,
        "transcript_folders": folders,
        "index_json_row": row,
        "index_md_block": entry.get("index_md_block"),
        "title": str(row.get("title") or "") if isinstance(row, dict) else "",
    }


# ------------------------------------------------------------------ restore
@dataclass(frozen=True)
class RestoreOutcome:
    date: str
    skill: str
    capture: str
    status: str
    reason: str = ""


def _parse_selector(selector: str) -> tuple[str, str, str]:
    parts = [p for p in selector.replace("\\", "/").strip("/").split("/") if p]
    if not 1 <= len(parts) <= 3 or not _DATE_RE.match(parts[0]):
        raise ValueError("restore takes <YYYY-MM-DD>[/<skill>[/<capture file>]]")
    parts += [""] * (3 - len(parts))
    for part in parts[1:]:
        if part and (Path(part).name != part or part in (".", "..")):
            raise ValueError("restore takes <YYYY-MM-DD>[/<skill>[/<capture file>]]")
    return parts[0], parts[1], parts[2]


def find_backups(root: Path, selector: str) -> List[tuple[str, str, str]]:
    """``(date, skill, capture)`` for every manifest the selector covers, oldest first."""
    day, skill, capture = _parse_selector(selector)
    day_dir = root / day
    if not day_dir.is_dir() or _is_link(day_dir):
        return []
    found: List[tuple[str, str, str]] = []
    for skill_dir in sorted(day_dir.iterdir()):
        if not skill_dir.is_dir() or _is_link(skill_dir) or (skill and skill_dir.name != skill):
            continue
        for manifest in sorted(skill_dir.glob(f"*{MANIFEST_SUFFIX}")):
            name = manifest.name[: -len(MANIFEST_SUFFIX)]
            if not manifest.is_file() or _is_link(manifest) or (capture and name != capture):
                continue
            found.append((day, skill_dir.name, name))
    return found


def list_archived(root: Path, query: str = "", skill: str = "") -> List[Dict[str, Any]]:
    """Every backed-up conversation, newest run first, from the manifests.

    ``query`` matches (case-insensitively) the title, the skill, the capture's
    name and the start of its text — a zombie is a greeting, so its text is a
    few lines. ``skill`` narrows to one skill. Nothing here touches the live
    search index: the backup stays invisible to the capture pipeline.
    """
    needle = query.strip().lower()
    try:
        days = sorted((c.name for c in root.iterdir() if _DATE_RE.match(c.name)), reverse=True)
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for day in days:
        for found_day, found_skill, capture in reversed(find_backups(root, day)):
            if skill and found_skill != skill:
                continue
            folder = root / found_day / found_skill
            try:
                manifest = json.loads((folder / f"{capture}{MANIFEST_SUFFIX}").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            title = " ".join(str(manifest.get("title") or "").split())
            if needle:
                haystack = " ".join((title, found_skill, capture, _capture_head(folder / capture))).lower()
                if needle not in haystack:
                    continue
            rows.append({
                "day": found_day, "skill": found_skill, "capture": capture, "title": title,
                "agent": str(manifest.get("agent", "")), "moved_at": str(manifest.get("moved_at", "")),
            })
            if len(rows) >= MAX_ARCHIVED_ROWS:
                return rows
    return rows


def _capture_head(path: Path) -> str:
    if not path.is_file() or _is_link(path):
        return ""
    try:
        with path.open("rb") as handle:
            return handle.read(_SEARCH_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _refuse(day: str, skill: str, capture: str, reason: str) -> RestoreOutcome:
    return RestoreOutcome(day, skill, capture, STATUS_REFUSED, reason)


def restore_one(cfg: WebappConfig, root: Path, day: str, skill: str, capture: str) -> RestoreOutcome:
    """Put one backed-up conversation back; refuse, touching nothing, on any conflict."""
    folder = root / day / skill
    try:
        manifest = json.loads((folder / f"{capture}{MANIFEST_SUFFIX}").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _refuse(day, skill, capture, "manifest unreadable")
    sid = str(manifest.get("sid", "")) if isinstance(manifest, dict) else ""
    if (not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION
            or manifest.get("skill") != skill or manifest.get("capture") != capture
            or Path(capture).name != capture or not capture.endswith(".md") or capture == "index.md"
            or (sid and not SESSION_ID_RE.fullmatch(sid))):
        return _refuse(day, skill, capture, "manifest does not match its location")
    backed_capture = folder / capture
    if not backed_capture.is_file() or _is_link(backed_capture):
        return _refuse(day, skill, capture, "capture missing from the backup")

    conv_dir = skills_dir_for(Path(cfg.life_os_dir)) / skill / "conversations"
    if not conv_dir.is_dir() or _is_link(conv_dir):
        return _refuse(day, skill, capture, "the skill's conversations folder is missing")
    target_capture = conv_dir / capture
    if target_capture.exists() or target_capture.is_symlink():
        return _refuse(day, skill, capture, "a capture with this name already exists")

    # Plan the transcript move first, so a conflict is found before anything moves.
    moves: List[tuple[Path, Path]] = [(backed_capture, target_capture)]
    transcript_parts = [p for p in (folder / f"{sid}.jsonl", folder / sid) if sid and (p.exists() or p.is_symlink())]
    if transcript_parts:
        project = str(manifest.get("transcript_project", ""))
        if not project or Path(project).name != project or project in (".", ".."):
            return _refuse(day, skill, capture, "the manifest does not say which project folder to restore into")
        project_dir = claude_projects_dir() / project
        if project_dir.exists() and (not project_dir.is_dir() or _is_link(project_dir)):
            return _refuse(day, skill, capture, "the project folder is a link or not a folder")
        for part in transcript_parts:
            if _is_link(part):
                return _refuse(day, skill, capture, "a backed-up transcript entry is a link")
            landing = project_dir / part.name
            if landing.exists() or landing.is_symlink():
                return _refuse(day, skill, capture, "a transcript with this id already exists")
            moves.append((part, landing))

    try:
        for source, landing in moves:
            landing.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(landing))
    except OSError as exc:
        logger.warning("⚠️ Life OS zombie restore: move failed for %s/%s: %s", skill, capture, exc)
        return RestoreOutcome(day, skill, capture, STATUS_FAILED, "a file could not be moved back")

    row = manifest.get("index_json_row")
    block = manifest.get("index_md_block")
    restore_index_entry(target_capture, row if isinstance(row, dict) else None,
                        block if isinstance(block, str) else None)
    title = " ".join(str(manifest.get("title") or "").split())
    if title and len(title) <= _MAX_TITLE_CHARS:
        try:
            proc = run_title_cli(cfg, skill, capture, title)
            if proc.returncode != 0:
                logger.warning("⚠️ Life OS zombie restore: title writer exited %s", proc.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("⚠️ Life OS zombie restore: could not restore the title: %s", exc)
    resync_search_index(cfg)
    try:
        (folder / f"{capture}{MANIFEST_SUFFIX}").unlink()
        for empty in (folder, folder.parent):
            if empty.is_dir() and not any(empty.iterdir()):
                empty.rmdir()
    except OSError:
        logger.warning("⚠️ Life OS zombie restore: backup folder cleanup unavailable")
    logger.info("ℹ️ Life OS zombie restored: skill=%s file=%s", skill, capture)
    return RestoreOutcome(day, skill, capture, STATUS_RESTORED)


# ---------------------------------------------------------------- retention
def purge_old_backups(root: Path, retention_days: int, today: Optional[date] = None) -> int:
    """Remove dated run folders older than ``retention_days``; returns how many.

    Only direct children of ``root`` whose name parses as ``YYYY-MM-DD`` are
    candidates; links, files and anything unparseable are left alone.
    """
    today = today or date.today()
    purged = 0
    try:
        children = sorted(root.iterdir())
    except OSError:
        return 0
    for child in children:
        if not _DATE_RE.match(child.name) or _is_link(child) or not child.is_dir():
            continue
        try:
            day = date.fromisoformat(child.name)
        except ValueError:
            continue
        if (today - day).days <= retention_days:
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:
            logger.warning("⚠️ Life OS zombie backup purge failed for %s: %s", child.name, exc)
            continue
        purged += 1
        logger.info("ℹ️ Life OS zombie backup purged: %s", child.name)
    return purged
