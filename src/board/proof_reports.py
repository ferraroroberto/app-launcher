"""Read-only view of fleet-config's visual-proof walkthrough store.

``ui_proof walkthrough`` (fleet-config) writes one private, self-contained HTML
per merged UI PR to ``<store>/<repo>-<PR>.html`` and records it in
``<store>/index.json`` under ``<repo>#<PR>``. The Board's Done lane links those
reports; this module only reads them and never writes, moves or deletes one.

A report holds real screenshots, so it never goes to or from GitHub. The lookup
is jailed to the store directory: the file name is rebuilt from a validated repo
name and an integer PR number, never taken from the index row's ``path``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Same override as fleet-config's walkthrough writer, so both sides agree.
STORE_ENV = "UI_WALKTHROUGH_DIR"
# A repo name as GitHub allows it; also keeps a separator or ".." out of a file name.
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# A report is a few hundred KB of inline PNGs; far past this is not one of ours.
MAX_REPORT_BYTES = 40 * 1024 * 1024

# What a PR body's ``VISUAL_PROOF=<state>`` line says when the proof is not there.
# ``fresh`` and ``n/a`` mean nothing is owed, so they are not listed.
OWED_STATES = frozenset({"missing", "stale", "failing", "unknown"})

PROOF_AVAILABLE = "available"
PROOF_MISSING = "missing"

_index_lock = threading.Lock()
_index_cache: Dict[str, Any] = {"sig": None, "rows": {}}


def store_dir() -> Path:
    configured = os.environ.get(STORE_ENV, "").strip()
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "fleet-ui-proof" / "walkthroughs"


def _read_index(folder: Path) -> Dict[str, Any]:
    """``index.json`` as a dict, re-read only when its size or mtime moved.

    The Board polls every 5 s, so an unchanged file costs one ``stat``. A
    missing or unreadable index is an empty one: the store may not exist yet.
    """
    path = folder / "index.json"
    try:
        stat = path.stat()
    except OSError:
        return {}
    sig: Tuple[str, int, int] = (str(path), stat.st_size, stat.st_mtime_ns)
    with _index_lock:
        if _index_cache["sig"] == sig:
            return _index_cache["rows"]
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ proof index unreadable: %s", exc)
        return {}
    if not isinstance(rows, dict):
        return {}
    with _index_lock:
        _index_cache["sig"] = sig
        _index_cache["rows"] = rows
    return rows


def report_path(repo: str, pr: int, folder: Optional[Path] = None) -> Optional[Path]:
    """The stored report for ``repo#pr``, or ``None`` when there is none.

    ``None`` covers every way the answer is no: a repo name that is not a plain
    repo name, no index row, a missing or oversized file, or a path that does
    not resolve to a regular file inside the store.
    """
    if not isinstance(repo, str) or not _REPO_NAME.match(repo):
        return None
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        return None
    root = folder if folder is not None else store_dir()
    if f"{repo}#{pr}" not in _read_index(root):
        return None
    candidate = root / f"{repo}-{pr}.html"
    try:
        resolved = candidate.resolve(strict=True)
        jail = root.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent != jail or not resolved.is_file():
        return None
    try:
        if resolved.stat().st_size > MAX_REPORT_BYTES:
            return None
    except OSError:
        return None
    return resolved


def annotate(items: List[Dict[str, Any]], folder: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Done cards with a ``proof`` field: ``available``, ``missing`` or absent.

    ``available`` is a stored report for the closing PR (the index is the
    authority). ``missing`` is the PR body declaring ``VISUAL_PROOF=<owed state>``
    with no report stored: the finisher says a UI diff shipped without proof.
    A card with neither stays without the field, since an index miss alone
    cannot tell a UI change from a docs change. ``proof_pr`` names the PR the
    report belongs to. The internal ``visual_proof`` marker is dropped.
    """
    root = folder if folder is not None else store_dir()
    out: List[Dict[str, Any]] = []
    for item in items:
        card = {k: v for k, v in item.items() if k != "visual_proof"}
        pr = item.get("pr")
        if isinstance(pr, int) and report_path(str(item.get("repo") or ""), pr, root) is not None:
            card["proof"] = PROOF_AVAILABLE
            card["proof_pr"] = pr
        elif isinstance(pr, int) and item.get("visual_proof") in OWED_STATES:
            card["proof"] = PROOF_MISSING
            card["proof_pr"] = pr
        out.append(card)
    return out
