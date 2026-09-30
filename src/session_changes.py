"""Changed files of one Claude session (#1349), folded from its transcript.

The session's ⋮ → *Changed files* lists every file its edit steps created,
modified or (best effort) deleted, with +/- counts, and opens each file into
the diffs of its own edits, in order. Deliberately built from the transcript
alone, never ``git diff``: the working tree mixes this session's edits with
other work in the same folder, and shows nothing once the work is committed
or the worktree is gone — the transcript is the durable record that still
holds after both.

Each successful Edit/Write/MultiEdit line carries Claude's own recorded diff
(``toolUseResult``, read by :func:`claude.recorded_edit` — the same reading
a Chat step takes, so the per-file totals are the sum of the per-step
counts). A failed step records an error string instead and never counts.

Bounded work per request: the scan reads at most :data:`SCAN_CEILING` bytes
(the newest part of the file, reported as ``partial``) and parses only lines
holding the ``structuredPatch`` marker — measured at 0.1 s for a 111 MB
transcript on the dev box. Claude only for now; other agents' edit tools are
read per step in Chat, not folded here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.transcript_flavors._shared import DIFF_FULL_BYTES, Hunk, _loads, cap_diff, diff_counts
from src.transcript_flavors.claude import recorded_edit

SCAN_CEILING = 256 * 1024 * 1024
_EDIT_MARKER = b'"structuredPatch"'

STATUS_ADDED = "A"
STATUS_MODIFIED = "M"
STATUS_DELETED = "D"


def _key(path: str) -> str:
    """Fold key for one file: Windows paths compare case-insensitively and
    either slash."""
    return path.replace("\\", "/").rstrip("/").lower()


def _edits(path: Path, needle: Optional[bytes] = None) -> Tuple[Iterator[Tuple[str, List[Hunk], bool, Any]], bool]:
    """``(edits, partial)``: every successful recorded edit in file order as
    ``(file path, hunks, created, timestamp)``, and whether the file was
    bigger than :data:`SCAN_CEILING` so only its newest part is read.
    ``needle`` pre-filters raw lines before parsing (one file's diff)."""
    size = path.stat().st_size
    start = max(0, size - SCAN_CEILING)

    def walk() -> Iterator[Tuple[str, List[Hunk], bool, Any]]:
        with path.open("rb") as fh:
            fh.seek(start)
            if start:
                fh.readline()   # the ceiling landed mid-line: skip the torn part
            for raw in fh:
                if _EDIT_MARKER not in raw or (needle is not None and needle not in raw):
                    continue
                obj = _loads(raw.decode("utf-8", errors="replace"))
                if obj is None or obj.get("type") != "user":
                    continue
                content = (obj.get("message") or {}).get("content")
                results = [b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_result"] \
                    if isinstance(content, list) else []
                if len(results) != 1 or results[0].get("is_error"):
                    continue
                edit = recorded_edit(obj.get("toolUseResult"))
                if edit is not None:
                    yield edit[0], edit[1], edit[2], obj.get("timestamp")

    return walk(), start > 0


def _display(path: str, project: Optional[str]) -> str:
    """``path`` relative to the project folder when it sits under it, else
    the full path — forward slashes either way."""
    shown = path.replace("\\", "/")
    if project:
        root = project.replace("\\", "/").rstrip("/")
        if shown.lower().startswith(root.lower() + "/"):
            return shown[len(root) + 1:]
    return shown


def changed_files(path: Path, project_dir: Optional[str]) -> Dict[str, Any]:
    """Every file the session's edits touched, folded per file.

    ``files`` rows are ``{path, key, status, additions, deletions, steps}``:
    ``path`` is what the panel shows, ``key`` the recorded path the diff
    route takes back. ``status`` is ``A`` when the session's first touch
    created the file, ``M`` otherwise, and ``D`` — best effort — when the
    file is gone from disk while the project folder is still there (with the
    folder gone, a missing file says nothing, so nothing is marked).
    """
    edits, partial = _edits(path)
    files: Dict[str, Dict[str, Any]] = {}
    for file_path, hunks, created, _ts in edits:
        row = files.get(_key(file_path))
        if row is None:
            row = files[_key(file_path)] = {
                "path": _display(file_path, project_dir), "key": file_path,
                "status": STATUS_ADDED if created else STATUS_MODIFIED,
                "additions": 0, "deletions": 0, "steps": 0,
            }
        added, removed = diff_counts(hunks)
        row["additions"] += added
        row["deletions"] += removed
        row["steps"] += 1
    project_exists = bool(project_dir) and Path(str(project_dir)).is_dir()
    if project_exists:
        for row in files.values():
            if not Path(row["key"]).exists():
                row["status"] = STATUS_DELETED
    rows = sorted(files.values(), key=lambda r: r["path"].lower())
    return {
        "files": rows,
        "counts": {
            "additions": sum(r["additions"] for r in rows),
            "deletions": sum(r["deletions"] for r in rows),
        },
        "partial": partial,
        "project_exists": project_exists,
    }


def file_steps(path: Path, key: str, max_bytes: int = DIFF_FULL_BYTES) -> Optional[Dict[str, Any]]:
    """The diffs of every edit the session made to ``key``, in order, all
    together capped at ``max_bytes``; None when the session never edited
    that file (so only a path the list handed out is ever answered)."""
    # The recorded path as it sits in the JSON line, to skip every other
    # edit before parsing. Only when it round-trips as plain ASCII: a line
    # may hold non-ASCII raw or \u-escaped, and a missed match would drop
    # a real step — without the needle every edit line is parsed instead.
    escaped = json.dumps(key)[1:-1]
    needle = escaped.encode("ascii") if escaped.isascii() and "\\u" not in escaped else None
    edits, partial = _edits(path, needle)
    target = _key(key)
    steps: List[Dict[str, Any]] = []
    budget = max_bytes
    truncated = False
    found = False
    for file_path, hunks, created, ts in edits:
        if _key(file_path) != target:
            continue
        found = True
        if truncated:
            continue
        diff = cap_diff(hunks, budget)
        used = sum(len(line.encode("utf-8", errors="replace")) + 1 for h in diff["hunks"] for line in h["lines"])
        budget -= used
        steps.append({"timestamp": ts, "created": created, "diff": diff})
        if diff["truncated"] or budget <= 0:
            truncated = True
    if not found:
        return None
    return {"steps": steps, "truncated": truncated, "partial": partial}
