"""Changed files of one agent session (#1349, #1356), folded from its transcript.

The session's ⋮ → *Changed files* lists every file its edit steps created,
modified or (best effort) deleted, with +/- counts, and opens each file into
the diffs of its own edits, in order. Deliberately built from the transcript
alone, never ``git diff``: the working tree mixes this session's edits with
other work in the same folder, and shows nothing once the work is committed
or the worktree is gone — the transcript is the durable record that still
holds after both.

Two folds share one output. **Claude** records its own diff after each
successful edit (``toolUseResult``, read by :func:`claude.recorded_edit` — the
same reading a Chat step takes), so its fold prefilters raw lines for that
marker: measured at 0.1 s for a 111 MB transcript on the dev box. **Every
other agent** records only the call and its result, so its fold runs the
flavour's own entry builder — the one Chat uses — and reads the ``edited`` /
``wrote`` / ``deleted`` steps it built, skipping any marked ``error``. Either
way the per-file totals are the sum of the per-step counts a Chat step shows,
by construction. Those diffs are worked out from the edit's own text, so most
carry no line numbers (:mod:`._shared`).

Bounded work per request: the Claude scan reads at most :data:`SCAN_CEILING`
bytes; the builder fold at most :data:`FLAVOR_SCAN_CEILING`, in windows of
:data:`FLAVOR_WINDOW` so memory stays flat. Both keep the *newest* part of the
file and report ``partial`` when they cut. Measured on the dev box (#1356): the
largest Codex rollout, 33.8 MB, builds whole in 0.22 s (~6.5 ms/MB), so the
64 MiB ceiling is ~0.4 s at worst; no rollout there passes it.
"""

from __future__ import annotations

import json
import posixpath
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

from src.session_transcript import FLAVORS
from src.transcript_flavors._shared import DIFF_FULL_BYTES, Hunk, _loads, cap_diff, diff_counts
from src.transcript_flavors.claude import recorded_edit

SCAN_CEILING = 256 * 1024 * 1024
FLAVOR_SCAN_CEILING = 64 * 1024 * 1024
FLAVOR_WINDOW = 16 * 1024 * 1024
_EDIT_MARKER = b'"structuredPatch"'
_EDIT_VERBS = frozenset({"edited", "wrote", "deleted"})

STATUS_ADDED = "A"
STATUS_MODIFIED = "M"
STATUS_DELETED = "D"


class Edit(NamedTuple):
    """One successful edit step, whichever agent made it."""
    path: str
    hunks: List[Hunk]
    created: bool
    ts: Any
    deleted: bool = False
    added: int = 0
    removed: int = 0
    truncated: bool = False    # the recorded diff was already cut at DIFF_FULL_BYTES


def _key(path: str) -> str:
    """Fold key for one file: Windows paths compare case-insensitively and
    either slash."""
    return path.replace("\\", "/").rstrip("/").lower()


def _claude_edits(path: Path, needle: Optional[bytes] = None) -> Tuple[Iterator[Edit], bool]:
    """``(edits, partial)``: every successful recorded edit in file order,
    and whether the file was bigger than :data:`SCAN_CEILING` so only its
    newest part is read. ``needle`` pre-filters raw lines before parsing
    (one file's diff)."""
    size = path.stat().st_size
    start = max(0, size - SCAN_CEILING)

    def walk() -> Iterator[Edit]:
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
                    added, removed = diff_counts(edit[1])
                    yield Edit(edit[0], edit[1], edit[2], obj.get("timestamp"), added=added, removed=removed)

    return walk(), start > 0


def _windows(path: Path, start: int) -> Iterator[List[Tuple[int, str]]]:
    """The complete lines of ``path`` from byte ``start`` on, as ``(byte
    offset, text)`` in groups of about :data:`FLAVOR_WINDOW` bytes."""
    with path.open("rb") as fh:
        fh.seek(start)
        pos = start
        if start:
            pos += len(fh.readline())   # the ceiling landed mid-line: skip the torn part
        chunk: List[Tuple[int, str]] = []
        used = 0
        for raw in fh:
            offset, pos = pos, pos + len(raw)
            if not raw.strip():
                continue
            chunk.append((offset, raw.decode("utf-8", errors="replace")))
            used += len(raw)
            if used >= FLAVOR_WINDOW:
                yield chunk
                chunk, used = [], 0
        if chunk:
            yield chunk


def _flavor_edits(path: Path, flavor: str) -> Tuple[Iterator[Edit], bool]:
    """``(edits, partial)`` from the flavour's own entry builder, in file
    order (#1356). A call's outcome can sit in the next window, so a
    successful-looking edit with no result yet is held back and its lines
    are read again with that window — never split from its result."""
    build = FLAVORS[flavor][0]
    size = path.stat().st_size
    start = max(0, size - FLAVOR_SCAN_CEILING)

    def walk() -> Iterator[Edit]:
        carried: List[Tuple[int, str]] = []
        seen: Set[Tuple[int, int]] = set()
        windows = _windows(path, start)
        window = next(windows, None)
        while window is not None:
            following = next(windows, None)
            final = following is None
            lines = carried + window
            entries = build(lines)
            per_line: Dict[int, int] = {}
            waiting: Optional[int] = None
            for e in entries:
                if e.get("kind") != "tool_call" or not isinstance(e.get("offset"), int):
                    continue
                offset = e["offset"]
                n = per_line.get(offset, 0)
                per_line[offset] = n + 1
                action = e.get("action")
                if not isinstance(action, dict) or action.get("verb") not in _EDIT_VERBS or (offset, n) in seen:
                    continue
                if e.get("result") is None and not final and lines[-1][0] - offset <= FLAVOR_WINDOW * 2:
                    waiting = offset if waiting is None else min(waiting, offset)
                    continue
                seen.add((offset, n))
                if e.get("error"):
                    continue
                edit = _edit_of(action, e.get("timestamp"))
                if edit is not None:
                    yield edit
            carried = [line for line in lines if waiting is not None and line[0] >= waiting]
            window = following

    return walk(), start > 0


def _edit_of(action: Dict[str, Any], ts: Any) -> Optional[Edit]:
    """The :class:`Edit` a built step stands for, or None for a step that
    changed no line (an edit with no diff — the Claude fold skips these too)."""
    path = action.get("path")
    if not isinstance(path, str) or not path:
        return None
    if action.get("verb") == "deleted":
        return Edit(path, [], False, ts, deleted=True)
    diff = action.get("diff")
    if not isinstance(diff, dict) or not diff.get("hunks"):
        return None
    return Edit(
        path, diff["hunks"], bool(action.get("created")), ts,
        added=int(action.get("added") or 0), removed=int(action.get("removed") or 0),
        truncated=bool(diff.get("truncated")),
    )


def _absolute(path: str) -> bool:
    return PureWindowsPath(path).is_absolute() or path.startswith(("/", "\\"))


def _resolved(path: str, project: Optional[str], flavor: str) -> str:
    """The path an edit names, made absolute against the project folder when
    the agent recorded it relative (a Codex patch names files from its
    working folder). Claude records absolute paths and is left untouched."""
    if flavor == "claude" or not project or _absolute(path):
        return path
    return posixpath.normpath(project.replace("\\", "/").rstrip("/") + "/" + path.replace("\\", "/"))


def _edits(path: Path, flavor: str, project: Optional[str],
           needle: Optional[bytes] = None) -> Tuple[Iterator[Edit], bool]:
    if flavor == "claude":
        return _claude_edits(path, needle)
    edits, partial = _flavor_edits(path, flavor)
    return (e._replace(path=_resolved(e.path, project, flavor)) for e in edits), partial


def _display(path: str, project: Optional[str]) -> str:
    """``path`` relative to the project folder when it sits under it, else
    the full path — forward slashes either way."""
    shown = path.replace("\\", "/")
    if project:
        root = project.replace("\\", "/").rstrip("/")
        if shown.lower().startswith(root.lower() + "/"):
            return shown[len(root) + 1:]
    return shown


def changed_files(path: Path, project_dir: Optional[str], flavor: str = "claude") -> Dict[str, Any]:
    """Every file the session's edits touched, folded per file.

    ``files`` rows are ``{path, key, status, additions, deletions, steps}``:
    ``path`` is what the panel shows, ``key`` the recorded path the diff
    route takes back. ``status`` is ``A`` when the session's first touch
    created the file, ``M`` otherwise, and ``D`` when a step deleted it or —
    best effort — when the file is gone from disk while the project folder is
    still there (with the folder gone, a missing file says nothing, so
    nothing is marked). A later edit of a deleted file makes it ``M`` again.
    """
    edits, partial = _edits(path, flavor, project_dir)
    files: Dict[str, Dict[str, Any]] = {}
    for edit in edits:
        row = files.get(_key(edit.path))
        if row is None:
            row = files[_key(edit.path)] = {
                "path": _display(edit.path, project_dir), "key": edit.path,
                "status": STATUS_ADDED if edit.created else STATUS_MODIFIED,
                "additions": 0, "deletions": 0, "steps": 0,
            }
        if edit.deleted:
            row["status"] = STATUS_DELETED
        elif row["status"] == STATUS_DELETED:
            row["status"] = STATUS_MODIFIED
        row["additions"] += edit.added
        row["deletions"] += edit.removed
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


def file_steps(path: Path, key: str, max_bytes: int = DIFF_FULL_BYTES,
               flavor: str = "claude", project_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The diffs of every edit the session made to ``key``, in order, all
    together capped at ``max_bytes``; None when the session never edited
    that file (so only a path the list handed out is ever answered)."""
    # The recorded path as it sits in the JSON line, to skip every other
    # edit before parsing. Only when it round-trips as plain ASCII: a line
    # may hold non-ASCII raw or \u-escaped, and a missed match would drop
    # a real step — without the needle every edit line is parsed instead.
    escaped = json.dumps(key)[1:-1]
    needle = escaped.encode("ascii") if escaped.isascii() and "\\u" not in escaped else None
    edits, partial = _edits(path, flavor, project_dir, needle)
    target = _key(key)
    steps: List[Dict[str, Any]] = []
    budget = max_bytes
    truncated = False
    found = False
    for edit in edits:
        if _key(edit.path) != target:
            continue
        found = True
        if truncated:
            continue
        diff = cap_diff(edit.hunks, budget)
        used = sum(len(line.encode("utf-8", errors="replace")) + 1 for h in diff["hunks"] for line in h["lines"])
        budget -= used
        step: Dict[str, Any] = {"timestamp": edit.ts, "created": edit.created, "diff": diff}
        if edit.deleted:
            step["deleted"] = True
        steps.append(step)
        if diff["truncated"] or edit.truncated or budget <= 0:
            truncated = True
    if not found:
        return None
    return {"steps": steps, "truncated": truncated, "partial": partial}
