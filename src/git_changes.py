"""Read-only working-tree inspection for the Coding tab's project menu (#977).

Three things the ``⋯`` menu on a Coding row needs that no editor should be
required for:

* :func:`list_changes` — every changed / untracked file of a project with
  its status letter, staged flag and ``+N −N`` counts (VS Code's Source
  Control list, GitHub's "Files changed" header).
* :func:`file_diff` — one file's unified diff, capped, for the phone-first
  accordion viewer.
* :func:`open_folder` — the project directory in Windows Explorer on the PC.

Everything git goes through :func:`src.git_utils.run_git` (argv list,
``NO_WINDOW``, ``None`` on a non-zero exit). That last property is why an
untracked file's diff is *synthesized* here rather than produced with
``git diff --no-index`` — that command exits ``1`` whenever the files differ,
which the shared runner reports as a failure.

Deliberately a viewer, never a second VS Code: nothing here stages, commits,
discards or writes to the working tree.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.git_utils import run_git
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

#: git's well-known empty-tree object — the diff base for a repository that
#: has no commit yet, so a fresh ``git init`` needs no special case anywhere.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: Per-file diff cap. A phone reads a few screens of diff; anything past
#: this is "open it in VS Code" territory and the response says so.
MAX_DIFF_BYTES = 200_000

#: Bytes sniffed for a NUL to call an untracked file binary.
_BINARY_SNIFF_BYTES = 8192

#: Status letters the SPA renders as badges (VS Code's vocabulary).
#: ``M`` modified · ``A`` added · ``D`` deleted · ``R`` renamed ·
#: ``T`` type change · ``U`` untracked · ``C`` conflict (unmerged).
STATUS_UNTRACKED = "U"
STATUS_CONFLICT = "C"

_PATH_SEP_RE = re.compile(r"[\\/]")
#: ``C:`` / ``C:\`` — a drive-qualified path is absolute whatever the host.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


# ------------------------------------------------------------------ models


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    staged: bool
    old_path: Optional[str] = None
    additions: Optional[int] = None
    deletions: Optional[int] = None
    binary: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "staged": self.staged,
            "old_path": self.old_path,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
        }


@dataclass(frozen=True)
class Changes:
    branch: Optional[str]
    files: List[ChangedFile]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch": self.branch,
            "files": [f.to_dict() for f in self.files],
            "counts": {
                "files": len(self.files),
                "additions": sum(f.additions or 0 for f in self.files),
                "deletions": sum(f.deletions or 0 for f in self.files),
            },
        }


@dataclass(frozen=True)
class FileDiff:
    path: str
    diff: str
    binary: bool = False
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "diff": self.diff,
            "binary": self.binary,
            "truncated": self.truncated,
        }


# ------------------------------------------------------------------ helpers


def is_git_repo(project_dir: Path) -> bool:
    """Same cheap test :func:`src.scanner.git_status` uses — no subprocess."""
    return (Path(project_dir) / ".git").exists()


def _base_ref(project_dir: Path) -> str:
    """``HEAD`` when it resolves, else the empty tree (no commit yet)."""
    if run_git(project_dir, ["rev-parse", "--verify", "--quiet", "HEAD"]) is None:
        return EMPTY_TREE
    return "HEAD"


def safe_relative_path(project_dir: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside ``project_dir`` or raise ``ValueError``.

    The path comes straight from the browser, so it is treated as hostile:
    it must be relative, free of ``.``/``..`` segments and empty segments,
    and its resolved form (symlinks followed) must stay under the resolved
    project root. Returns the resolved absolute path.
    """
    text = str(rel_path or "").strip()
    if not text:
        raise ValueError("empty path")
    if text.startswith(("/", "\\")) or _DRIVE_RE.match(text) or Path(text).is_absolute():
        raise ValueError("absolute path")
    parts = _PATH_SEP_RE.split(text)
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("path escapes the project")
    root = Path(project_dir).resolve()
    target = (root / Path(*parts)).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes the project")
    return target


def _sniff(target: Path, limit: int) -> Tuple[bytes, bool, bool]:
    """Read up to ``limit + 1`` bytes: ``(data, is_binary, over_limit)``."""
    with open(target, "rb") as fh:
        head = fh.read(_BINARY_SNIFF_BYTES)
        if b"\0" in head:
            return head, True, False
        rest = fh.read(max(0, limit + 1 - len(head)))
    data = head + rest
    return data, False, len(data) > limit


def _split_lines(text: str) -> Tuple[List[str], bool]:
    """``(lines, ends_with_newline)`` — a trailing newline adds no line."""
    if not text:
        return [], True
    ends = text.endswith("\n")
    lines = text.split("\n")
    if ends:
        lines.pop()
    return lines, ends


def _synth_untracked_diff(rel_path: str, text: str) -> str:
    """A unified diff that adds every line of a not-yet-tracked file."""
    lines, ends = _split_lines(text)
    out = [f"--- /dev/null\n+++ b/{rel_path}\n@@ -0,0 +1,{len(lines)} @@\n"]
    out.extend("+" + line + "\n" for line in lines)
    if lines and not ends:
        out.append("\\ No newline at end of file\n")
    return "".join(out)


def _truncate(diff: str, max_bytes: int) -> Tuple[str, bool]:
    """Cut ``diff`` to ``max_bytes`` on a line boundary."""
    raw = diff.encode("utf-8")
    if len(raw) <= max_bytes:
        return diff, False
    cut = raw[:max_bytes].decode("utf-8", errors="ignore")
    head, sep, _tail = cut.rpartition("\n")
    return (head + sep) if sep else cut, True


# ------------------------------------------------------------ list_changes


def _parse_numstat(out: Optional[str]) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
    """``git diff --numstat -z`` → ``{path: (added, deleted)}``.

    With ``-z`` a rename record is ``added\\tdeleted\\t`` followed by two more
    NUL-separated records (old path, new path); a binary file reports
    ``-\\t-``, mapped to ``(None, None)``.
    """
    counts: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    if not out:
        return counts
    records = out.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        fields = rec.split("\t", 2)
        if len(fields) < 3:
            continue
        added_s, deleted_s, path = fields
        if path == "":
            # rename: the two following records are old and new path
            if i + 1 < len(records):
                path = records[i + 1]
            i += 2
        added = None if added_s == "-" else int(added_s)
        deleted = None if deleted_s == "-" else int(deleted_s)
        counts[path] = (added, deleted)
    return counts


def _untracked_counts(target: Path) -> Tuple[Optional[int], Optional[int], bool]:
    """``(additions, deletions, binary)`` for a file git doesn't know yet."""
    try:
        data, binary, _over = _sniff(target, MAX_DIFF_BYTES)
    except OSError as exc:
        logger.debug("⚠️ git_changes: cannot read %s: %s", target, exc)
        return None, None, False
    if binary:
        return None, None, True
    lines, _ends = _split_lines(data.decode("utf-8", errors="replace"))
    return len(lines), 0, False


def list_changes(project_dir: Path) -> Changes:
    """Every changed / untracked file of ``project_dir`` versus its base.

    One ``git status --porcelain=v2 -z --untracked-files=all`` for the file
    set and status letters, one ``git diff <base> -M --numstat -z`` for the
    per-file counts. The base is ``HEAD`` (or the empty tree before the
    first commit), so the list is "what a commit would contain" — the
    question the red tile name asks — with a ``staged`` flag rather than
    VS Code's two separate Staged / Changes panes.
    """
    project_dir = Path(project_dir)
    out = run_git(project_dir, ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all"])
    if out is None:
        return Changes(branch=None, files=[])
    base = _base_ref(project_dir)
    counts = _parse_numstat(run_git(project_dir, ["diff", base, "-M", "--numstat", "-z"]))

    branch: Optional[str] = None
    files: List[ChangedFile] = []
    records = out.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# branch.head "):
            head = rec[len("# branch.head "):].strip()
            branch = None if head == "(detached)" else head
            continue
        if rec.startswith("#"):
            continue
        kind = rec[0]
        if kind == "?":
            path = rec[2:]
            added, deleted, binary = _untracked_counts(project_dir / path)
            files.append(ChangedFile(
                path=path, status=STATUS_UNTRACKED, staged=False,
                additions=added, deletions=deleted, binary=binary,
            ))
            continue
        if kind == "1":
            parts = rec.split(" ", 8)
            if len(parts) < 9:
                continue
            xy, path, old_path = parts[1], parts[8], None
        elif kind == "2":
            parts = rec.split(" ", 9)
            if len(parts) < 10:
                continue
            xy, path = parts[1], parts[9]
            old_path = records[i] if i < len(records) else None
            i += 1
        elif kind == "u":
            parts = rec.split(" ", 10)
            if len(parts) < 11:
                continue
            xy, path, old_path = "uu", parts[10], None
        else:
            continue
        x, y = xy[0], xy[1] if len(xy) > 1 else "."
        if kind == "u":
            status = STATUS_CONFLICT
        else:
            status = y if y != "." else x
            if status == "C":  # a copy is a rename to the reader
                status = "R"
        added, deleted = counts.get(path, (None, None))
        binary = path in counts and added is None
        files.append(ChangedFile(
            path=path, status=status, staged=(x != "." and kind != "u"),
            old_path=old_path, additions=added, deletions=deleted, binary=binary,
        ))

    files.sort(key=lambda f: (f.status == STATUS_UNTRACKED, f.path.lower()))
    return Changes(branch=branch, files=files)


# --------------------------------------------------------------- file_diff


def file_diff(project_dir: Path, rel_path: str, max_bytes: int = MAX_DIFF_BYTES) -> FileDiff:
    """Unified diff of one file versus the base ref, capped at ``max_bytes``.

    Tracked files (modified, staged, deleted, renamed) come from
    ``git diff <base> -M`` — exit 0 without ``--exit-code``, so the shared
    runner is happy. A file git doesn't track yet is read directly and
    rendered as an all-``+`` hunk; a NUL in its first 8 KiB makes it
    ``binary`` with an empty diff. Raises ``ValueError`` for a path that
    isn't a clean relative path inside the project.
    """
    project_dir = Path(project_dir)
    target = safe_relative_path(project_dir, rel_path)
    rel = "/".join(_PATH_SEP_RE.split(str(rel_path).strip()))
    base = _base_ref(project_dir)

    out = run_git(project_dir, ["diff", base, "-M", "--no-color", "--no-ext-diff", "--", rel])
    if out:
        binary = "\nBinary files " in ("\n" + out)
        diff, truncated = _truncate(out + "\n", max_bytes)
        return FileDiff(path=rel, diff=diff, binary=binary, truncated=truncated)

    untracked = run_git(project_dir, ["ls-files", "--others", "--exclude-standard", "--", rel])
    if not untracked or not target.is_file():
        return FileDiff(path=rel, diff="")
    try:
        data, binary, over = _sniff(target, max_bytes)
    except OSError as exc:
        raise OSError(f"could not read {rel}: {exc}") from exc
    if binary:
        return FileDiff(path=rel, diff="", binary=True)
    text = data.decode("utf-8", errors="replace")
    diff, truncated = _truncate(_synth_untracked_diff(rel, text), max_bytes)
    return FileDiff(path=rel, diff=diff, truncated=truncated or over)


# ------------------------------------------------------------- open_folder


def open_folder(project_dir: Path) -> int:
    """Open ``project_dir`` in Windows Explorer; return the spawned PID.

    Same argv-list / ``NO_WINDOW`` / ``close_fds`` shape as
    :func:`src.vscode_workspace.open_workspace`. Explorer is its own
    top-level app (and exits non-zero by design even on success), so
    nothing waits on it or tracks it afterwards. A non-Windows host has no
    Explorer: ``OSError`` so the route can answer 503 with the reason.
    """
    if os.name != "nt":
        raise OSError("open folder is Windows-only")
    exe = shutil.which("explorer") or "explorer.exe"
    proc = subprocess.Popen(
        [exe, str(project_dir)],
        shell=False,
        creationflags=NO_WINDOW,
        close_fds=True,
    )
    logger.info(f"📂 opened folder in Explorer: {project_dir} (pid {proc.pid})")
    return proc.pid
