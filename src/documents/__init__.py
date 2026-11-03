"""Settings > Documents: the fleet's sources of truth, read from disk.

The owner lists documents in ``webapp_config.json`` as ``{id, title, repo,
path}`` rows. A browser never names a path: it asks for an ``id`` from this
list, and this module is the only place an ``id`` becomes a file. Two layers
keep the read inside the fleet:

- **Entry validation** (:func:`parse_documents`): ``repo`` is one bare folder
  name, ``path`` a relative ``.md`` path with no ``..``, drive letter or
  backslash. A row that fails is dropped with a warning; the rest still load.
- **The jail** (:func:`read_document`, :func:`last_changed`): the file is
  resolved under ``<projects_dir>/<repo>`` and refused unless its real path
  stays inside that folder, so a symlink pointing out is refused too.

``repo`` is the primary checkout's folder name, so a sibling worktree
(``<repo>-wt-N``) is never read. Logic layer: no UI imports.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# A document is prose a phone reads; past this the endpoint says so instead of
# cutting it off mid-table.
MAX_DOCUMENT_BYTES = 512 * 1024

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# One bare folder name: no separator, no leading dot (so no ``.``/``..``/``.git``).
_REPO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
# ``<repo>-wt-<N>`` is an isolated worktree of ``<repo>``, never the checkout
# that holds the merged document.
_WORKTREE_RE = re.compile(r"-wt-\d+$")
_MAX_TITLE = 120


@dataclass(frozen=True)
class Document:
    """One validated allowlist entry."""

    id: str
    title: str
    repo: str
    path: str


class DocumentError(Exception):
    """A read that cannot be served; ``kind`` says which condition.

    Distinct kinds so the phone can tell a missing file from an oversize one:
    ``not_found`` / ``outside`` / ``too_large`` / ``unreadable``.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _reject(index: int, why: str) -> None:
    logger.warning("⚠️  documents[%d] dropped: %s", index, why)


def _path_problem(path: str) -> Optional[str]:
    if not path or path != path.strip():
        return "path is empty or padded with spaces"
    if "\\" in path:
        return "path must use forward slashes"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "path must be relative (no leading slash or drive letter)"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "path must not contain '..', '.' or empty segments"
    if not path.lower().endswith(".md"):
        return "path must end in .md"
    return None


def parse_documents(raw: Any) -> List[Document]:
    """The allowlist from the config's ``documents`` value, bad rows dropped."""
    if not isinstance(raw, list):
        return []
    out: List[Document] = []
    seen: set[str] = set()
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            _reject(index, "not an object")
            continue
        ident, title = row.get("id"), row.get("title")
        repo, path = row.get("repo"), row.get("path")
        if not all(isinstance(v, str) for v in (ident, title, repo, path)):
            _reject(index, "id, title, repo and path must all be text")
            continue
        title = title.strip()
        problem = None
        if not _ID_RE.match(ident):
            problem = "id must be a short lowercase slug"
        elif ident in seen:
            problem = f"id {ident!r} is used twice"
        elif not title or len(title) > _MAX_TITLE:
            problem = "title is empty or too long"
        elif not _REPO_RE.match(repo) or _WORKTREE_RE.search(repo):
            problem = "repo must be one primary-checkout folder name"
        else:
            problem = _path_problem(path)
        if problem:
            _reject(index, problem)
            continue
        seen.add(ident)
        out.append(Document(ident, title, repo, path))
    return out


def find_document(documents: Iterable[Document], doc_id: str) -> Optional[Document]:
    return next((d for d in documents if d.id == doc_id), None)


def _resolve(projects_dir: str, doc: Document) -> Path:
    """The document's real path, or a :class:`DocumentError`.

    Both the repo folder and the file are resolved (symlinks and junctions
    followed) and must stay under their parent, so nothing outside the fleet
    root is ever opened.
    """
    try:
        root = Path(projects_dir).resolve()
        repo_dir = (root / doc.repo).resolve()
        target = (repo_dir / doc.path).resolve()
    except (OSError, ValueError) as exc:
        raise DocumentError("unreadable", f"could not resolve the path: {exc}")
    if repo_dir.parent != root:
        raise DocumentError("outside", "the repo folder is outside the fleet root")
    try:
        target.relative_to(repo_dir)
    except ValueError:
        raise DocumentError("outside", "the file resolves outside its repo")
    if not target.is_file():
        raise DocumentError("not_found", "file not found")
    return target


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def last_changed(projects_dir: str, doc: Document) -> Optional[str]:
    """The file's modification time (UTC ISO), or ``None`` when it cannot be read.

    A plain ``stat``, no ``git`` spawn: a pull rewrites only files whose
    content changed, so this tracks real changes on this host.
    """
    try:
        return _iso(_resolve(projects_dir, doc).stat().st_mtime)
    except (DocumentError, OSError):
        return None


def list_documents(projects_dir: str, documents: Iterable[Document]) -> List[Dict[str, Any]]:
    """One row per document for the Settings list; ``updated`` is ``None`` when unreadable."""
    return [
        {
            "id": d.id,
            "title": d.title,
            "repo": d.repo,
            "updated": last_changed(projects_dir, d),
        }
        for d in documents
    ]


def read_document(projects_dir: str, doc: Document) -> Dict[str, Any]:
    """The document's text and metadata, or a :class:`DocumentError`."""
    target = _resolve(projects_dir, doc)
    try:
        with target.open("rb") as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
        updated = _iso(target.stat().st_mtime)
    except OSError as exc:
        raise DocumentError("unreadable", f"could not read the file: {exc}")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentError(
            "too_large",
            f"document is over the {MAX_DOCUMENT_BYTES // 1024} KB limit",
        )
    return {
        "id": doc.id,
        "title": doc.title,
        "repo": doc.repo,
        "updated": updated,
        "content": raw.decode("utf-8", errors="replace"),
        "bytes": len(raw),
    }
