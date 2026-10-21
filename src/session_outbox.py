"""Files an agent sends to the owner's chat view, any harness (#1530).

An agent in a launcher session posts ``{path, caption}`` to its session's
outbox (``POST /api/claude-code/sessions/{sid}/outbox``, loopback-only). The
file is checked here, **copied** into the launcher's own runtime store, and
listed for the Chat pane, which renders it inline (image, PDF, text) under
its caption. Copying at post time means what the owner sees is what the
agent sent, whatever happens to the source afterwards.

Claude's own ``SendUserFile`` tool call reaches the same chat through the
transcript instead (``/transcript/file``); its listed paths go through
:func:`read_source`, the same guards, read live from disk.

**Guards**, each refusing with its own :class:`OutboxRefusal` reason:

* ``out_of_root`` — the path, fully resolved (``..``, symlinks and
  junctions included), must sit under the session's own folder or a declared
  scratch root (``outbox_scratch_roots``). Compared by path components, so
  ``proj-evil`` is not inside ``proj``.
* ``not_found`` / ``not_a_file`` — nothing there, or a directory.
* ``too_large`` — over :data:`MAX_FILE_BYTES`, checked on the stat *and* on
  the bytes actually read, so a file growing under the read cannot slip past.
* ``type_not_allowed`` — the extension is not on :data:`ALLOWED_TYPES`.
* ``content_mismatch`` — the bytes are not what the extension claims (a
  ``.png`` that is not a PNG, a ``.txt`` that is not UTF-8 text). The served
  media type comes from this check, never from the caller.

Store: ``<audit dir>/outbox/<sid>/`` — ``index.json`` plus one file per
entry, named by a launcher-generated id. Follows ``LAUNCHER_AUDIT_DIR`` like
the dispatch briefs (#1114), so a test or gate run never writes into the
checkout's live ``webapp/``. Bounded: a session keeps its newest
:data:`MAX_ENTRIES_PER_SESSION`, and every post prunes sessions untouched for
:data:`OUTBOX_TTL`.

Webapp-only: not on the session-host import closure (CLAUDE.md
"session-host"), so a change here never needs a ``:8446`` restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src._json_io import atomic_write_json
from src.audit import AUDIT_DIR_ENV, PROJECT_ROOT
from src.image_sniff import IMAGE_MAGIC, sniff_image

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_CAPTION_CHARS = 500
MAX_NAME_CHARS = 200
MAX_ENTRIES_PER_SESSION = 50
OUTBOX_TTL = 7 * 24 * 3600

# Extension -> kind. Nothing that a browser would run (HTML, SVG) and nothing
# it would sniff into something else: an image is served as the type its own
# bytes prove, a PDF as a PDF, and every text kind as text/plain.
ALLOWED_TYPES: Dict[str, str] = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".pdf": "pdf",
    ".txt": "text", ".log": "text",
    ".md": "markdown", ".markdown": "markdown",
}
TEXT_MEDIA = "text/plain; charset=utf-8"
PDF_MEDIA = "application/pdf"

# Every media type a sent file is ever served as. The image types come from
# session_transcript.IMAGE_MAGIC — one magic-byte table for both the
# transcript's inline images and this outbox (#1627).
SERVED_MEDIA = frozenset(
    [media for _, media in IMAGE_MAGIC] + ["image/webp", PDF_MEDIA, TEXT_MEDIA]
)
# A session-host id or an entry id: both launcher-made. Anything else never
# becomes a directory or file name here.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# One webapp process writes the store; this keeps two posts to one session
# from interleaving their read-modify-write of its index.
_LOCK = threading.Lock()


class OutboxRefusal(ValueError):
    """A file the outbox will not take, with a machine-readable ``reason``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _resolve_outbox_dir() -> Path:
    # Mirrors src.audit's resolution of the same variable (#913).
    override = os.environ.get(AUDIT_DIR_ENV, "").strip()
    return (Path(override) if override else PROJECT_ROOT / "webapp") / "outbox"


OUTBOX_DIR = _resolve_outbox_dir()


# ------------------------------------------------------------------ guards


def _is_network_or_device(text: str) -> bool:
    """A UNC or device path (``\\\\host\\share``, ``\\\\?\\``, ``\\\\.\\pipe``),
    judged on the text alone."""
    return text.replace("/", "\\").startswith("\\\\")


def resolve_root(raw: str) -> Path:
    """The fully resolved folder a declared root names (``%VAR%`` and ``~``
    expanded). Raises :class:`OutboxRefusal` for a network or device path,
    refused before any filesystem call (resolving one makes Windows open an
    SMB connection), and for a path that does not resolve. The one place a
    root becomes a path, so the Settings editor and the send guards cannot
    disagree about what a root means."""
    text = os.path.expanduser(os.path.expandvars(str(raw or "").strip()))
    if not text:
        raise OutboxRefusal("bad_request", "no folder given")
    if _is_network_or_device(text):
        raise OutboxRefusal("out_of_root", "network and device paths are not accepted")
    try:
        return Path(text).resolve(strict=True)
    except (OSError, RuntimeError):
        raise OutboxRefusal("not_found", "the folder does not exist") from None


def check_root(raw: str, others: Iterable[str] = (), *, forgive_missing: bool = False) -> None:
    """Raises ``ValueError`` (one distinct message per condition) unless
    ``raw`` is a folder worth saving as a send root: not a network or device
    path, not a drive root, existing, a folder rather than a file, and not
    the same folder as an entry of ``others``. ``forgive_missing`` lets an
    entry that was already saved pass when its folder has since gone."""
    label = str(raw or "").strip()
    try:
        resolved = resolve_root(raw)
    except OutboxRefusal as exc:
        if exc.reason == "bad_request":
            raise ValueError("Enter a folder path.") from None
        if exc.reason == "out_of_root":
            raise ValueError(f"{label}: network and device paths are not accepted.") from None
        if forgive_missing:
            return
        raise ValueError(f"{label}: that folder does not exist.") from None
    if resolved == Path(resolved.anchor):
        raise ValueError(f"{label}: a whole drive is too broad; pick a folder on it.")
    if not resolved.is_dir():
        raise ValueError(f"{label}: that is a file, not a folder.")
    for other in others:
        try:
            twin = resolve_root(other)
        except OutboxRefusal:
            continue
        if os.path.normcase(str(twin)) == os.path.normcase(str(resolved)):
            raise ValueError(f"{label}: that folder is already in the list.")


def allowed_roots(project_dir: Optional[str], scratch_roots: Iterable[str]) -> List[Path]:
    """The resolved folders a sent file may come from: the session's own
    folder, then each declared scratch root (``%VAR%`` and ``~`` expanded).
    A root that does not resolve is left out rather than guessed at."""
    roots: List[Path] = []
    for raw in [project_dir or "", *scratch_roots]:
        if not str(raw or "").strip():
            continue
        try:
            roots.append(resolve_root(raw))
        except OutboxRefusal as exc:
            logger.info("ℹ️ outbox root not usable, skipped (%s): %s", exc.reason, Path(str(raw)).name)
    return roots


def _inside(path: Path, root: Path) -> bool:
    """Is ``path`` ``root`` or below it, compared component by component
    (case-insensitively where the OS is)? A string prefix would count
    ``proj-evil`` as inside ``proj``."""
    p = os.path.normcase(str(path))
    r = os.path.normcase(str(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


def sniff_media(kind: str, data: bytes) -> Optional[str]:
    """The media type ``data`` proves for ``kind``, or None when it doesn't."""
    if kind == "image":
        return sniff_image(data)
    if kind == "pdf":
        return PDF_MEDIA if data.startswith(b"%PDF-") else None
    if kind in ("text", "markdown"):
        if b"\x00" in data:
            return None
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return TEXT_MEDIA
    return None


def kind_of(name: str) -> Optional[str]:
    """The kind a file name's extension allows, else None."""
    return ALLOWED_TYPES.get(Path(name).suffix.lower())


def read_source(
    path: str, base_dir: Optional[str], roots: List[Path]
) -> Tuple[Path, bytes, str, str]:
    """``(resolved path, bytes, kind, media type)`` of a file the guards
    allow, else raises :class:`OutboxRefusal`.

    ``path`` may be relative: it is taken against ``base_dir`` (the agent's
    working folder). The checks run on the resolved path, in an order that
    never reads a byte of a file outside the roots.
    """
    text = str(path or "").strip()
    if not text:
        raise OutboxRefusal("bad_request", "no path given")
    candidate = Path(text)
    if not candidate.is_absolute():
        if not base_dir:
            raise OutboxRefusal("bad_request", "relative path with no working folder to resolve it against")
        candidate = Path(base_dir) / candidate
    # Before any filesystem call: resolving a UNC or device path
    # (`\\host\share`, `\\?\`, `\\.\pipe`) makes Windows open it, which for a
    # remote host means an SMB/WebDAV connection that hands it the user's
    # NTLM hash. A path from a transcript is model-written, so refuse lexically.
    if _is_network_or_device(str(candidate)):
        raise OutboxRefusal("out_of_root", "network and device paths are not accepted")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise OutboxRefusal("not_found", "no file at that path") from None
    except (OSError, RuntimeError):
        raise OutboxRefusal("not_found", "the path does not resolve") from None
    if not roots or not any(_inside(resolved, root) for root in roots):
        raise OutboxRefusal(
            "out_of_root", "the file is outside the session's folder and every declared scratch root"
        )
    if not resolved.is_file():
        raise OutboxRefusal("not_a_file", "the path is not a regular file")
    kind = kind_of(resolved.name)
    if kind is None:
        raise OutboxRefusal(
            "type_not_allowed",
            "only images (png, jpg, gif, webp), PDF, text and markdown files can be sent",
        )
    limit_mb = MAX_FILE_BYTES // (1024 * 1024)
    try:
        if resolved.stat().st_size > MAX_FILE_BYTES:
            raise OutboxRefusal("too_large", f"the file is over the {limit_mb} MB limit")
        with resolved.open("rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise OutboxRefusal("not_found", "the file could not be read") from None
    if len(data) > MAX_FILE_BYTES:
        raise OutboxRefusal("too_large", f"the file is over the {limit_mb} MB limit")
    media = sniff_media(kind, data)
    if media is None:
        raise OutboxRefusal("content_mismatch", f"the file's contents are not a valid {kind} file")
    return resolved, data, kind, media


# ------------------------------------------------------------------- store


def _session_dir(sid: str) -> Path:
    if not _SAFE_ID.match(sid or ""):
        raise OutboxRefusal("bad_request", "not a session id")
    return OUTBOX_DIR / sid


def _read_index(folder: Path) -> Dict[str, Any]:
    try:
        raw = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"rev": 0, "entries": []}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ outbox index unreadable for %s: %s", folder.name[:8], exc.__class__.__name__)
        return {"rev": 0, "entries": []}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    rev = raw.get("rev") if isinstance(raw, dict) else None
    return {
        "rev": rev if isinstance(rev, int) else 0,
        "entries": [e for e in entries or [] if isinstance(e, dict)],
    }


def _prune_sessions(now: float) -> None:
    """Drop every session folder untouched for :data:`OUTBOX_TTL`."""
    try:
        folders = [p for p in OUTBOX_DIR.iterdir() if p.is_dir()]
    except OSError:
        return
    for folder in folders:
        try:
            stale = now - folder.stat().st_mtime > OUTBOX_TTL
        except OSError:
            continue
        if stale:
            shutil.rmtree(folder, ignore_errors=True)
            logger.info("ℹ️ outbox pruned an expired session folder %s", folder.name[:8])


def _clean_caption(caption: Any) -> str:
    text = str(caption or "").strip()
    return text[:MAX_CAPTION_CHARS]


def clean_name(name: str) -> str:
    """A file name fit to show: control and format characters (bidi
    overrides included) out, length bounded. The client still escapes it."""
    cleaned = "".join(ch for ch in name if ch.isprintable())
    return cleaned[:MAX_NAME_CHARS] or "file"


def post(
    sid: str,
    path: str,
    caption: Any,
    base_dir: Optional[str],
    roots: List[Path],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Check ``path``, copy it into ``sid``'s outbox and return its entry.
    Raises :class:`OutboxRefusal` (nothing is written) when a guard refuses."""
    folder = _session_dir(sid)
    _resolved, data, kind, media = read_source(path, base_dir, roots)
    stamp = time.time() if now is None else now
    entry_id = uuid.uuid4().hex
    entry = {
        "id": entry_id,
        "name": clean_name(_resolved.name),
        "caption": _clean_caption(caption),
        "kind": kind,
        "media": media,
        "size": len(data),
        "timestamp": datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with _LOCK:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        _prune_sessions(stamp)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / entry_id).write_bytes(data)
        index = _read_index(folder)
        entries = index["entries"] + [entry]
        for old in entries[:-MAX_ENTRIES_PER_SESSION]:
            old_id = str(old.get("id") or "")
            if _SAFE_ID.match(old_id):
                (folder / old_id).unlink(missing_ok=True)
        entries = entries[-MAX_ENTRIES_PER_SESSION:]
        atomic_write_json(folder / "index.json", {"rev": index["rev"] + 1, "entries": entries})
    logger.info(
        "ℹ️ outbox %s: %s %d bytes (%s)", sid[:8], kind, len(data), entry_id[:8],
    )
    return entry


def listing(sid: str) -> Dict[str, Any]:
    """``{"rev", "entries"}`` for ``sid`` — rev 0 and nothing for a session
    that never sent a file. ``rev`` grows with every post, so a live view
    can tell "unchanged" without comparing lists."""
    try:
        folder = _session_dir(sid)
    except OutboxRefusal:
        return {"rev": 0, "entries": []}
    if not (folder / "index.json").is_file():
        return {"rev": 0, "entries": []}
    return _read_index(folder)


def entry_file(sid: str, entry_id: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """``(stored file, entry)`` for one listed entry, else None."""
    if not _SAFE_ID.match(entry_id or ""):
        return None
    try:
        folder = _session_dir(sid)
    except OutboxRefusal:
        return None
    entry = next((e for e in listing(sid)["entries"] if e.get("id") == entry_id), None)
    stored = folder / entry_id
    if entry is None or not stored.is_file():
        return None
    return stored, entry
