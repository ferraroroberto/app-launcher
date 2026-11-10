"""Settings › Inspiration: the /inspiration-scan source list, edited from the app.

fleet-config's ``inspiration-scan`` skill owns the file format and the file
(``sources.toml`` in its machine-local state root). This module loads the
skill's own ``sources.py`` by path (the load :mod:`src.fleet_scope` uses) and
asks its ``parse_config`` to validate every document before it is written, so
the app never carries a second copy of the format.

The list names the people and publications the owner follows, so it stays
private: no name, URL or note is ever logged, only counts.

A save replaces the three source lists and the profile note, keeps every other
key as it was (``[settings]``, unknown tables, unknown keys on an entry),
writes the file atomically and leaves the previous text beside it as
``sources.toml.bak``. A missing, old or unloadable skill is its own state,
``available: False`` with a reason, never an empty list.
"""

from __future__ import annotations

import copy
import hashlib
import html
import logging
import re
import threading
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from src import runtime_files
from src._fleet_contract import load_fleet_contract
from src._log_once import log_once
from src.inspiration.toml_write import TomlWriteError, dumps

logger = logging.getLogger(__name__)

SKILL_SUBPATH = Path(".claude") / "skills" / "inspiration-scan"
BACKUP_SUFFIX = ".bak"

MAX_ENTRIES = 200
MAX_TEXT = 500
MAX_LIKES = 8000
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_DIGEST_HEAD_BYTES = 64 * 1024
_DIGEST_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.html$")

# The two shapes the digest header takes: "N ideas from M items read" and,
# with no source list, the "not filled in yet" card.
_KEPT = re.compile(r'<p class="sub">(\d+) ideas? from (\d+) items? read</p>')
_CHIP = re.compile(r'<li class="chip (ok|warn)">(.*?)</li>', re.S)

_logged_failures: set[str] = set()
_FAILURE_LOG_CAP = 64
_save_lock = threading.Lock()


class InspirationError(Exception):
    """A request the app refuses; ``status`` is the HTTP code it maps to."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


def _load(fleet_config_dir: str) -> Tuple[Optional[ModuleType], str]:
    """The skill's ``sources`` module, or ``(None, reason)`` when it cannot serve."""
    if not str(fleet_config_dir or "").strip():
        return None, "The fleet-config folder is not set in Settings."
    path = Path(fleet_config_dir) / SKILL_SUBPATH / "sources.py"
    if not path.is_file():
        return None, "This fleet-config checkout has no inspiration-scan skill yet."
    try:
        module = load_fleet_contract(str(path.resolve()), "launcher_inspiration_sources", "inspiration-sources")
        for name in ("parse_config", "ConfigError", "sources_path", "state_root"):
            getattr(module, name)
    except Exception as exc:  # a broken checkout must not break Settings
        log_once(
            _logged_failures, str(exc), _FAILURE_LOG_CAP, logger.warning,
            "⚠️ inspiration sources: cannot load %s: %s", path, exc,
        )
        return None, "The inspiration-scan skill could not be loaded."
    return module, ""


def state_root_for(fleet_config_dir: str) -> Tuple[Optional[Path], str]:
    """The scan's machine-local state root, or ``(None, reason)`` when the skill cannot serve."""
    module, reason = _load(fleet_config_dir)
    return (None, reason) if module is None else (Path(module.state_root()), "")


def _revision(raw: Optional[bytes]) -> str:
    return "" if raw is None else hashlib.sha256(raw).hexdigest()[:16]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def digest_status(root: Path) -> Dict[str, Any]:
    """What the newest digest says: date, ideas kept, and one chip per source type."""
    digests = root / "digests"
    try:
        names = sorted(p.name for p in digests.iterdir() if _DIGEST_NAME.match(p.name))
    except OSError:
        names = []
    if not names:
        return {"state": "none"}
    name = names[-1]
    date = name[: -len(".html")]
    try:
        with (digests / name).open("rb") as handle:
            head = handle.read(_DIGEST_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError as exc:
        log_once(_logged_failures, str(exc), _FAILURE_LOG_CAP, logger.warning,
                 "⚠️ inspiration sources: cannot read the latest digest: %s", exc)
        return {"state": "unreadable", "date": date}
    if "Source list not filled in yet" in head:
        return {"state": "unfilled", "date": date}
    kept = _KEPT.search(head)
    if kept is None:
        return {"state": "unreadable", "date": date}
    chips = [{"text": html.unescape(re.sub(r"<[^>]+>", "", text)).strip(), "ok": cls == "ok"}
             for cls, text in _CHIP.findall(head)]
    return {"state": "ok", "date": date, "kept": int(kept.group(1)), "items": int(kept.group(2)), "chips": chips}


def _read_document(path: Path) -> Tuple[Optional[bytes], Optional[Dict[str, Any]], str]:
    """``(raw, parsed, problem)``: a missing file is ``(None, {}, "")``, an unparseable one ``(raw, None, why)``."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}, ""
    except OSError as exc:
        return None, None, f"{path.name} cannot be read: {exc.strerror or exc}"
    try:
        return raw, tomllib.loads(raw.decode("utf-8")), ""
    except UnicodeDecodeError:
        return raw, None, f"{path.name} is not valid UTF-8 text."
    except tomllib.TOMLDecodeError as exc:
        return raw, None, f"{path.name} is not valid TOML: {exc}"


def _entries(data: Dict[str, Any], key: str) -> Optional[List[Dict[str, Any]]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        return None
    return value


def _profile(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = data.get("profile", {})
    return value if isinstance(value, dict) else None


def _view(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The editable part of a document, or ``None`` when its shape cannot be edited here."""
    lists = {key: _entries(data, key) for key in ("newsletter", "youtube", "x")}
    profile = _profile(data)
    if profile is None or any(v is None for v in lists.values()):
        return None
    return {
        "likes": _text(profile.get("likes")),
        "newsletter": [{"index": i, "name": _text(e.get("name")), "feed": _text(e.get("feed"))}
                       for i, e in enumerate(lists["newsletter"] or [])],
        "youtube": [{"index": i, "name": _text(e.get("name")), "channel_id": _text(e.get("channel_id"))}
                    for i, e in enumerate(lists["youtube"] or [])],
        "x": [{"index": i, "handle": _text(e.get("handle")).lstrip("@")}
              for i, e in enumerate(lists["x"] or [])],
    }


def read_state(fleet_config_dir: str) -> Dict[str, Any]:
    """Everything the Settings sheet draws. Blocking: wrap in a thread."""
    module, reason = _load(fleet_config_dir)
    if module is None:
        return _unavailable(reason)
    path = module.sources_path()
    raw, data, problem = _read_document(path)
    state: Dict[str, Any] = {
        "available": True, "reason": "", "exists": raw is not None, "revision": _revision(raw),
        "editable": False, "problem": problem, "digest": digest_status(module.state_root()),
        "likes": "", "newsletter": [], "youtube": [], "x": [],
    }
    if data is None:
        return state
    view = _view(data)
    if view is None:
        state["problem"] = f"{path.name} has a table the app cannot edit; fix it by hand."
        return state
    state.update(view)
    state["editable"] = True
    try:
        module.parse_config(data, path.parent)
    except module.ConfigError as exc:
        state["problem"] = str(exc)
    return state


def _clean(value: Any, label: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise InspirationError(f"{label} must be text.")
    text = value.strip()
    if not text:
        raise InspirationError(f"{label} is required.")
    if len(text) > limit:
        raise InspirationError(f"{label} is longer than {limit} characters.")
    if any(ord(c) < 0x20 for c in text):
        raise InspirationError(f"{label} must be one line of text.")
    return text


def _incoming(payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    rows = payload.get(key, [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise InspirationError(f"{key} must be a list.")
    if len(rows) > MAX_ENTRIES:
        raise InspirationError(f"At most {MAX_ENTRIES} {key} entries.")
    return rows


def _check_new_entry(kind: str, row: Dict[str, Any]) -> Dict[str, str]:
    """The fields of one incoming entry, trimmed and checked; the message names the entry."""
    if kind == "newsletter":
        name = _clean(row.get("name"), "A newsletter name")
        feed = _clean(row.get("feed"), f"The feed of {name!r}")
        if not re.match(r"^https?://\S+$", feed):
            raise InspirationError(f"The feed of {name!r} must be a web address starting with http:// or https://.")
        return {"name": name, "feed": feed}
    if kind == "youtube":
        name = _clean(row.get("name"), "A YouTube channel name")
        channel = _clean(row.get("channel_id"), f"The channel id of {name!r}")
        if not _CHANNEL_ID.match(channel):
            raise InspirationError(
                f"The channel id of {name!r} must start with UC and be 24 characters "
                "(from the channel page: Share > Copy channel ID)."
            )
        return {"name": name, "channel_id": channel}
    handle = _clean(row.get("handle"), "An X handle").lstrip("@")
    if not _X_HANDLE.match(handle):
        raise InspirationError(f"{handle!r} is not an X handle: letters, digits and underscore, up to 15.")
    return {"handle": handle}


_IDENTITY = {"newsletter": "feed", "youtube": "channel_id", "x": "handle"}


def _merge_entries(kind: str, old: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The new list: each row's shown fields over the entry its ``index`` names, so unshown keys survive."""
    merged: List[Dict[str, Any]] = []
    used: set[int] = set()
    seen: set[str] = set()
    for row in rows:
        fields = _check_new_entry(kind, row)
        identity = fields[_IDENTITY[kind]].lower()
        if identity in seen:
            raise InspirationError(f"{fields[_IDENTITY[kind]]!r} is listed twice.")
        seen.add(identity)
        index = row.get("index")
        base: Dict[str, Any] = {}
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(old):
            if index in used:
                raise InspirationError("An entry was sent twice.")
            used.add(index)
            base = dict(old[index])
        merged.append({**base, **fields})
    return merged


def _build_document(data: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    likes = payload.get("likes", "")
    if not isinstance(likes, str):
        raise InspirationError("The profile note must be text.")
    likes = likes.strip()
    if len(likes) > MAX_LIKES:
        raise InspirationError(f"The profile note is longer than {MAX_LIKES} characters.")
    doc = copy.deepcopy(data)
    profile = _profile(doc)
    if profile is None:
        raise InspirationError("The file's [profile] is not a table; fix it by hand.", 409)
    profile["likes"] = likes
    doc["profile"] = profile
    for kind in ("newsletter", "youtube", "x"):
        old = _entries(doc, kind)
        if old is None:
            raise InspirationError(f"The file's [[{kind}]] is not a list of tables; fix it by hand.", 409)
        merged = _merge_entries(kind, old, _incoming(payload, kind))
        if merged:
            doc[kind] = merged
        else:
            doc.pop(kind, None)
    return doc


def _write(path: Path, raw_old: Optional[bytes], text: str) -> None:
    """Backup, then swap the new text over the file; the file is whole or untouched."""
    runtime_files.make_dirs(path.parent)
    if raw_old is not None:
        runtime_files.save_bytes(path.with_name(path.name + BACKUP_SUFFIX), raw_old)
    temp = path.with_name(path.name + ".tmp")
    try:
        runtime_files.save_bytes(temp, text.encode("utf-8"))
        runtime_files.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        logger.warning("⚠️ inspiration sources: write failed: %s", exc.strerror or exc)
        raise InspirationError(f"{path.name} could not be written ({exc.strerror or exc}). Try again.", 409)


def save(fleet_config_dir: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the profile note and the three source lists; returns the fresh state.

    Raises :class:`InspirationError` and leaves the file untouched on any refusal.
    """
    module, reason = _load(fleet_config_dir)
    if module is None:
        raise InspirationError(reason, 503)
    path = module.sources_path()
    with _save_lock:
        raw, data, problem = _read_document(path)
        if data is None:
            raise InspirationError(f"{problem} Fix the file by hand first; nothing was changed.", 409)
        if payload.get("revision", "") != _revision(raw):
            raise InspirationError("The file changed since this sheet loaded. Close and reopen it, then try again.", 409)
        doc = _build_document(data, payload)
        try:
            text = dumps(doc)
            if tomllib.loads(text) != doc:
                raise TomlWriteError("the written text does not read back the same")
            module.parse_config(tomllib.loads(text), path.parent)
        except module.ConfigError as exc:
            raise InspirationError(str(exc)) from exc
        except TomlWriteError as exc:
            raise InspirationError(f"The list could not be written safely: {exc}", 500) from exc
        _write(path, raw, text)
    logger.info("✅ inspiration sources saved: %d newsletters, %d channels, %d handles",
                len(doc.get("newsletter", [])), len(doc.get("youtube", [])), len(doc.get("x", [])))
    return read_state(fleet_config_dir)
