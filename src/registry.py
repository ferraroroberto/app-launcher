"""Unified app registry — load, save, scan, mutate.

The registry is one JSON file (``config/apps.json``, gitignored) that
holds every launchable thing the hub knows about, across both tabs:

    {
      "scan_root": "E:\\automation",
      "apps": [
        {"id": "...", "name": "...", "kind": "claude-code", "project_dir": "..."},
        {"id": "...", "name": "...", "kind": "streamlit",   "bat_path": "..."},
        {"id": "...", "name": "...", "kind": "webapp",      "bat_path": "..."},
        {"id": "...", "name": "...", "kind": "tunnel",      "bat_path": "..."}
      ]
    }

Rows with ``kind == "claude-code"`` carry ``project_dir`` (a folder the
``claude`` CLI is cwd'd into). Every other kind carries ``bat_path``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .scanner import (
    KIND_CLAUDE_CODE,
    KIND_STREAMLIT,
    VALID_KINDS,
    app_id_from_path,
    pretty_folder_name,
    scan_app_bats,
    scan_claude_code_projects,
    tunnel_url_for,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "apps.json"


@dataclass
class AppEntry:
    id: str
    name: str
    kind: str
    bat_path: Optional[str] = None
    project_dir: Optional[str] = None
    added_at: str = ""

    def to_dict(self) -> Dict:
        payload: Dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "added_at": self.added_at,
        }
        if self.bat_path is not None:
            payload["bat_path"] = self.bat_path
        if self.project_dir is not None:
            payload["project_dir"] = self.project_dir
        return payload


@dataclass
class Registry:
    scan_root: str
    apps: List[AppEntry] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "scan_root": self.scan_root,
            "apps": [a.to_dict() for a in self.apps],
        }


def load_registry(path: Optional[Path] = None) -> Registry:
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    if not target.exists():
        return Registry(scan_root="")

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"⚠️  Could not read {target} ({exc}); starting fresh")
        return Registry(scan_root="")

    apps: List[AppEntry] = []
    for row in raw.get("apps") or []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or KIND_STREAMLIT)
        if kind not in VALID_KINDS:
            logger.warning(f"⚠️  Skipping app row with unknown kind: {row}")
            continue
        apps.append(
            AppEntry(
                id=str(row.get("id") or ""),
                name=str(row.get("name") or ""),
                kind=kind,
                bat_path=row.get("bat_path"),
                project_dir=row.get("project_dir"),
                added_at=str(row.get("added_at") or ""),
            )
        )
    return Registry(scan_root=str(raw.get("scan_root") or ""), apps=apps)


def save_registry(reg: Registry, path: Optional[Path] = None) -> Path:
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(reg.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


# ----------------------------------------------------------- decoration


def decorate_for_api(entry: AppEntry) -> Dict:
    """Render-time fields the API surfaces but the registry doesn't store.

    For ``tunnel`` rows, attaches the current public URL read from
    ``<bat.parent>/webapp/last_tunnel_url.txt``. Returns the API shape:

        {id, name, kind, bat_path?, project_dir?, added_at, tunnel_url?}
    """
    payload = entry.to_dict()
    if entry.kind == "tunnel" and entry.bat_path:
        payload["tunnel_url"] = tunnel_url_for(Path(entry.bat_path))
    return payload


# ----------------------------------------------------------- scan + diff


def discover_new(
    *, projects_dir: Path, scan_root: Path, existing: Registry
) -> List[AppEntry]:
    """Run both scanners and return entries not already in ``existing``.

    Each returned entry has ``added_at`` empty — the caller is expected
    to stamp it when persisting.
    """
    have_ids: set[str] = {a.id for a in existing.apps}
    have_paths: set[str] = {a.bat_path for a in existing.apps if a.bat_path}
    have_project_dirs: set[str] = {
        a.project_dir for a in existing.apps if a.project_dir
    }

    new: List[AppEntry] = []

    # Claude-Code projects (one row per project, no bat path on the entry).
    for project in scan_claude_code_projects(projects_dir):
        if project.id in have_ids:
            continue
        # Don't double-add when the user manually edited the file.
        if str(project.project_dir) in have_project_dirs:
            continue
        new.append(
            AppEntry(
                id=project.id,
                name=project.name,
                kind=KIND_CLAUDE_CODE,
                project_dir=str(project.project_dir),
            )
        )

    # Apps (streamlit / webapp / tunnel).
    for bat, kind in scan_app_bats(scan_root):
        if str(bat) in have_paths:
            continue
        new.append(
            AppEntry(
                id=app_id_from_path(bat, scan_root),
                name=pretty_folder_name(bat.parent),
                kind=kind,
                bat_path=str(bat),
            )
        )

    return new


def persist_additions(
    reg: Registry, additions: List[AppEntry], scan_root: Path
) -> List[AppEntry]:
    """Merge ``additions`` into ``reg`` and write to disk. Returns added rows."""
    have_ids: set[str] = {a.id for a in reg.apps}
    added: List[AppEntry] = []
    stamp = datetime.now().isoformat(timespec="seconds")
    for entry in additions:
        if entry.id in have_ids:
            continue
        entry.added_at = entry.added_at or stamp
        reg.apps.append(entry)
        added.append(entry)
    reg.scan_root = str(scan_root)
    reg.apps.sort(key=lambda a: a.name.lower())
    save_registry(reg)
    return added


def remove_by_id(reg: Registry, app_id: str) -> Optional[AppEntry]:
    """Drop the entry with ``id == app_id``. Returns the removed entry."""
    for i, entry in enumerate(reg.apps):
        if entry.id == app_id:
            removed = reg.apps.pop(i)
            save_registry(reg)
            return removed
    return None


def rename_by_id(reg: Registry, app_id: str, new_name: str) -> Optional[AppEntry]:
    for entry in reg.apps:
        if entry.id == app_id:
            entry.name = new_name
            reg.apps.sort(key=lambda a: a.name.lower())
            save_registry(reg)
            return entry
    return None


def get_by_id(reg: Registry, app_id: str) -> Optional[AppEntry]:
    return next((a for a in reg.apps if a.id == app_id), None)
