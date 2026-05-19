"""Content-hash stamping for /static assets.

The webapp ships ``index.html`` + a handful of ES-module ``.js`` files +
``styles.css``. iOS Safari (and especially the standalone PWA) caches
those aggressively, so a deploy isn't really "live" until the cached
copies are evicted. To make that deterministic we append ``?v=<hash>``
to every asset URL. The hash is computed once at app startup from the
content of the static dir; tray restart on every code edit (project
convention) means we don't need a watcher.

We use a single **fleet hash** — sha256 over the concatenation of each
file's per-file hash, sorted by name. Reasons:

  * The ES-module graph has a cycle (``sessions.js`` ↔ ``terminal.js``)
    so per-file transitive hashing would need SCC handling — overkill
    for ~10 files totalling ~150 KB.
  * The asset budget is tiny: any one edit re-downloads all hashed
    files on next visit, which is still well under a second on LTE.
  * One value to log and to surface from ``/api/version`` for visual
    diff against the deployed PC build.

Functions are pure and easy to unit-test in isolation.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, Iterable, Optional

_HASH_LEN = 8

# Files under static/ that get hashed + long-cached. Everything else
# (icons, manifest, mobileconfig, the xterm vendor bundle) is cached
# more conservatively by the static-files mount itself.
_HASHED_SUFFIXES = (".js", ".css")

# Subdirectories under static/ to skip entirely (vendor xterm is huge
# and immutable per upstream version — its URL never changes so it
# doesn't benefit from a content hash).
_SKIP_DIRS = ("vendor",)

_JS_IMPORT_RE = re.compile(
    r"""(from\s*['"])\./([\w\-.]+\.js)(\?v=[^'"]*)?(['"])"""
)

_INDEX_ASSET_RE = re.compile(
    r"""(href|src)=(['"])/static/([\w\-.]+\.(?:css|js))(\?v=[^'"]*)?(['"])"""
)


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:_HASH_LEN]


def _iter_hashable_files(static_dir: Path) -> Iterable[Path]:
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(static_dir).parts[:-1]):
            continue
        if path.suffix.lower() not in _HASHED_SUFFIXES:
            continue
        yield path


def compute_asset_hashes(static_dir: Path) -> Dict[str, str]:
    """Return ``{filename: fleet_hash}`` for every hashable static file.

    Same hash for every file (the fleet hash). Caller can still look
    up by filename — useful for future per-file hashing if the import
    graph ever loses its cycle. ``fleet_hash`` is the sha256-over-
    sha256s described in the module docstring.
    """
    if not static_dir.exists():
        return {}
    per_file: Dict[str, str] = {}
    for path in _iter_hashable_files(static_dir):
        per_file[path.name] = _short_hash(path.read_bytes())
    if not per_file:
        return {}
    fleet_input = "\n".join(
        f"{name}:{per_file[name]}" for name in sorted(per_file)
    ).encode("utf-8")
    fleet_hash = _short_hash(fleet_input)
    return {name: fleet_hash for name in per_file}


def fleet_hash_of(hashes: Dict[str, str]) -> str:
    """Single representative hash. Empty string if no assets."""
    if not hashes:
        return ""
    # By construction every value in ``hashes`` is the same fleet hash;
    # just return one. Resilient to an empty dict.
    return next(iter(hashes.values()))


def rewrite_js_imports(body: str, hashes: Dict[str, str]) -> str:
    """Stamp ``?v=<hash>`` onto every ``from './foo.js'`` import.

    Imports that don't have a matching entry in ``hashes`` are left as
    they were — robust against new files that haven't been added to
    the hash map yet (e.g. dynamic imports). Existing ``?v=…`` is
    replaced so re-rewriting a server-stamped body is idempotent.
    """
    if not hashes:
        return body

    def _sub(match: re.Match) -> str:
        prefix, filename, _existing, quote_close = match.group(1, 2, 3, 4)
        stamp = hashes.get(filename)
        if not stamp:
            return match.group(0)
        return f"{prefix}./{filename}?v={stamp}{quote_close}"

    return _JS_IMPORT_RE.sub(_sub, body)


def rewrite_index_html(body: str, hashes: Dict[str, str]) -> str:
    """Stamp ``?v=<hash>`` onto every ``/static/<file>.(css|js)`` href/src.

    Same robustness rules as ``rewrite_js_imports`` — unknown files
    pass through unchanged; existing version queries are replaced.
    """
    if not hashes:
        return body

    def _sub(match: re.Match) -> str:
        attr, quote_open, filename, _existing, quote_close = match.group(1, 2, 3, 4, 5)
        stamp = hashes.get(filename)
        if not stamp:
            return match.group(0)
        return f'{attr}={quote_open}/static/{filename}?v={stamp}{quote_close}'

    return _INDEX_ASSET_RE.sub(_sub, body)


def asset_hash_for(hashes: Dict[str, str], name: str) -> Optional[str]:
    """Lookup helper that survives an empty map without raising."""
    if not hashes:
        return None
    return hashes.get(name)
