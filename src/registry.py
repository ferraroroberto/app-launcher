"""Unified app registry — load, save, scan, mutate.

The registry is one JSON file (``config/apps.json``, gitignored) that
holds the **Apps tab** rows — bat-based launchers:

    {
      "scan_root": "E:\\automation",
      "apps": [
        {"id": "...", "name": "...", "kind": "streamlit", "bat_path": "..."},
        {"id": "...", "name": "...", "kind": "webapp",    "bat_path": "..."},
        {"id": "...", "name": "...", "kind": "tunnel",    "bat_path": "..."}
      ]
    }

``claude-code`` rows are **not** persisted here — they are computed live
on every request by :func:`live_claude_code_entries`, which scans the
configured ``projects_dir`` for project directories. Each carries a
``project_dir`` (the folder ``claude`` is cwd'd into); the Apps-tab
kinds each carry a ``bat_path``. Stale ``claude-code`` rows left in an
older ``apps.json`` are ignored by the API.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from . import runtime_files
from ._json_io import atomic_write_json
from .scanner import (
    KIND_CLAUDE_CODE,
    KIND_MINI,
    KIND_STREAMLIT,
    VALID_KINDS,
    app_id_from_path,
    github_repo_url,
    pretty_folder_name,
    scan_app_bats,
    scan_mini_bats,
    scan_project_dirs,
    slugify,
    tunnel_url_for,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "apps.json"


class RegistryReadError(Exception):
    """Raised by ``load_registry(strict=True)`` when ``config/apps.json``
    exists but could not be read or parsed.

    A missing file legitimately means "empty registry" (first run, nothing
    scanned in yet) for every caller — that case never raises this, even in
    strict mode. This is only for the narrower "the file is there but a read
    or parse failed" case, which non-strict callers still fold into an empty
    ``Registry`` (unchanged, so the Apps-tab scan/save flow keeps its existing
    tolerance) but which a strict caller needs to distinguish from a
    legitimately empty/unconfigured registry.
    """


class AmbiguousAppId(Exception):
    """More than one registry row carries the id a by-id call was given.

    Acting on the first match could change or remove the wrong app, so the
    by-id helpers refuse instead and the API answers 409 with this message.
    """

    def __init__(self, app_id: str, count: int) -> None:
        self.app_id = app_id
        self.count = count
        super().__init__(
            f"app id {app_id!r} matches {count} apps; "
            "the registry needs unique ids before this can act on one"
        )


@dataclass
class AppEntry:
    id: str
    name: str
    kind: str
    bat_path: Optional[str] = None
    project_dir: Optional[str] = None
    repo_url: Optional[str] = None
    added_at: str = ""
    # Whether the tray boots this ``tray`` or ``mini`` row automatically.
    # Meaningless on every other kind; always serialized (like ``added_at``)
    # rather than conditionally, since it's a plain bool with a real default,
    # not an optional field.
    autostart: bool = False

    def to_dict(self) -> Dict:
        payload: Dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "added_at": self.added_at,
            "autostart": self.autostart,
        }
        if self.bat_path is not None:
            payload["bat_path"] = self.bat_path
        if self.project_dir is not None:
            payload["project_dir"] = self.project_dir
        if self.repo_url is not None:
            payload["repo_url"] = self.repo_url
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


def load_registry(path: Optional[Path] = None, *, strict: bool = False) -> Registry:
    """Load the registry from ``path`` (default ``config/apps.json``).

    ``strict=True`` raises :class:`RegistryReadError` when the file exists but
    fails to read/parse, instead of silently folding that failure into an
    empty registry. Default (``strict=False``, every existing caller) keeps
    the original behavior — a missing file, or a read/parse failure, both
    just return an empty ``Registry``. Only ``app.tray.registered_trays``'s
    boot-time autostart walk needs to tell "genuinely nothing configured"
    apart from "couldn't read the file this time"; the Apps-tab
    scan/save flow and the CLI's ``scan`` command are unaffected.
    """
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    if not target.exists():
        return Registry(scan_root="")

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise RegistryReadError(f"could not read {target}: {exc}") from exc
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
                repo_url=row.get("repo_url"),
                added_at=str(row.get("added_at") or ""),
                autostart=bool(row.get("autostart")),
            )
        )
    reg = Registry(scan_root=str(raw.get("scan_root") or ""), apps=apps)
    if _migrate_duplicate_ids(reg):
        # Saved so the new ids are stable: the next load finds nothing to do.
        try:
            save_registry(reg, target)
        except OSError as exc:
            logger.warning(f"⚠️  Could not save the migrated app ids to {target}: {exc}")
    return reg


def unique_app_id(base: str, taken: Set[str]) -> str:
    """``base`` when free, else ``base-2``, ``base-3``... the first id not in ``taken``."""
    if base and base not in taken:
        return base
    stem = base or "app"
    n = 2
    while f"{stem}-{n}" in taken:
        n += 1
    return f"{stem}-{n}"


def _migrate_duplicate_ids(reg: Registry) -> bool:
    """Give every later row sharing an id a unique one, in place.

    The first row keeps its id. Returns ``True`` when any row changed.
    """
    taken = {a.id for a in reg.apps}
    seen: Set[str] = set()
    changed = False
    for entry in reg.apps:
        if entry.id and entry.id not in seen:
            seen.add(entry.id)
            continue
        new_id = unique_app_id(entry.id, taken)
        logger.warning(
            f"⚠️  duplicate app id {entry.id!r} on {entry.bat_path or entry.name!r}; "
            f"renamed to {new_id!r}"
        )
        entry.id = new_id
        taken.add(new_id)
        seen.add(new_id)
        changed = True
    return changed


def save_registry(reg: Registry, path: Optional[Path] = None) -> Path:
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    runtime_files.make_dirs(target.parent)
    atomic_write_json(target, reg.to_dict())
    return target


# ----------------------------------------------------------- decoration


def decorate_for_api(entry: AppEntry) -> Dict:
    """Render-time fields the API surfaces but the registry doesn't store.

    For ``tunnel`` rows, attaches the current public URL read from
    ``<bat.parent>/webapp/last_tunnel_url.txt``. Returns the API shape:

        {id, name, kind, bat_path?, project_dir?, repo_url?, added_at, tunnel_url?}
    """
    payload = entry.to_dict()
    if entry.kind == "tunnel" and entry.bat_path:
        payload["tunnel_url"] = tunnel_url_for(Path(entry.bat_path))
    return payload


# ------------------------------------------------------ claude-code (live)


def live_claude_code_entries(
    projects_dir: Path, ignore: Optional[List[str]] = None
) -> List[AppEntry]:
    """Build ``claude-code`` rows live from the directories under ``projects_dir``.

    No persistence and no scan step — every direct child directory of
    ``projects_dir`` that survives the ignore list (see
    :func:`src.scanner.scan_project_dirs`) becomes a launchable row.
    """
    return [
        AppEntry(
            id=project.id,
            name=project.name,
            kind=KIND_CLAUDE_CODE,
            project_dir=str(project.project_dir),
            repo_url=github_repo_url(project.project_dir),
        )
        for project in scan_project_dirs(projects_dir, ignore)
    ]


# ----------------------------------------------------------- scan + diff


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def discover_new(
    *,
    scan_root: Path,
    existing: Registry,
    mini_root: Optional[Path] = None,
) -> List[AppEntry]:
    """Scan ``scan_root`` for app bats (and ``mini_root`` for mini apps) not
    already in ``existing``.

    Each returned entry has ``added_at`` empty — the caller is expected
    to stamp it when persisting. ``claude-code`` rows are never returned
    here; they come from :func:`live_claude_code_entries` instead. A bat in
    ``mini_root`` is a ``mini`` row even when ``scan_root`` also covers that
    folder, so one file never lists twice. New rows start with autostart off.
    """
    have_paths: set[str] = {a.bat_path for a in existing.apps if a.bat_path}

    taken_ids: set[str] = {a.id for a in existing.apps}
    new: List[AppEntry] = []
    for bat, kind in scan_app_bats(scan_root):
        if str(bat) in have_paths:
            continue
        if mini_root is not None and _same_folder(bat.parent, mini_root):
            continue
        # A bat outside the scan root slugs to its bare file name, so two of
        # them can share an id; each candidate gets its own.
        new_id = unique_app_id(app_id_from_path(bat, scan_root), taken_ids)
        taken_ids.add(new_id)
        new.append(
            AppEntry(
                id=new_id,
                name=pretty_folder_name(bat.parent),
                kind=kind,
                bat_path=str(bat),
            )
        )

    if mini_root is not None:
        for bat in scan_mini_bats(mini_root):
            if str(bat) in have_paths:
                continue
            mini_id = unique_app_id(f"{KIND_MINI}-{slugify(bat.stem)}", taken_ids)
            taken_ids.add(mini_id)
            new.append(
                AppEntry(
                    id=mini_id,
                    name=pretty_folder_name(Path(bat.stem)),
                    kind=KIND_MINI,
                    bat_path=str(bat),
                )
            )

    return new


def persist_additions(
    reg: Registry, additions: List[AppEntry], scan_root: Path
) -> List[AppEntry]:
    """Merge ``additions`` into ``reg`` and write to disk. Returns added rows."""
    have_ids: set[str] = {a.id for a in reg.apps}
    have_paths: set[str] = {a.bat_path for a in reg.apps if a.bat_path}
    added: List[AppEntry] = []
    stamp = datetime.now().isoformat(timespec="seconds")
    for entry in additions:
        if entry.bat_path and entry.bat_path in have_paths:
            continue
        # A different script under a taken id is a new app, not a repeat.
        entry.id = unique_app_id(entry.id, have_ids)
        have_ids.add(entry.id)
        if entry.bat_path:
            have_paths.add(entry.bat_path)
        entry.added_at = entry.added_at or stamp
        reg.apps.append(entry)
        added.append(entry)
    reg.scan_root = str(scan_root)
    reg.apps.sort(key=lambda a: a.name.lower())
    save_registry(reg)
    return added


def _find_unique(reg: Registry, app_id: str) -> Optional[int]:
    """Index of the one row with ``app_id``, ``None`` when none has it.

    Raises :class:`AmbiguousAppId` when several do.
    """
    hits = [i for i, a in enumerate(reg.apps) if a.id == app_id]
    if len(hits) > 1:
        raise AmbiguousAppId(app_id, len(hits))
    return hits[0] if hits else None


def remove_by_id(reg: Registry, app_id: str) -> Optional[AppEntry]:
    """Drop the entry with ``id == app_id``. Returns the removed entry."""
    i = _find_unique(reg, app_id)
    if i is None:
        return None
    removed = reg.apps.pop(i)
    save_registry(reg)
    return removed


def rename_by_id(reg: Registry, app_id: str, new_name: str) -> Optional[AppEntry]:
    i = _find_unique(reg, app_id)
    if i is None:
        return None
    entry = reg.apps[i]
    entry.name = new_name
    reg.apps.sort(key=lambda a: a.name.lower())
    save_registry(reg)
    return entry


def set_autostart_by_id(
    reg: Registry, app_id: str, autostart: bool
) -> Optional[AppEntry]:
    """Flip the autostart flag for ``app_id``. Meaningless on an entry that is
    neither a ``tray`` nor a ``mini`` one, but not rejected — the API layer
    scopes which rows offer the toggle."""
    i = _find_unique(reg, app_id)
    if i is None:
        return None
    entry = reg.apps[i]
    entry.autostart = autostart
    save_registry(reg)
    return entry


def get_by_id(reg: Registry, app_id: str) -> Optional[AppEntry]:
    i = _find_unique(reg, app_id)
    return None if i is None else reg.apps[i]
