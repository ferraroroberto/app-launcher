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
import posixpath
import re
from pathlib import Path
from typing import Dict, Iterable, Optional

_HASH_LEN = 8

# Files under static/ whose content feeds the fleet hash. .js/.css get
# the hash stamped on their URLs server-side; .svg (the brand icons) is
# stamped client-side via dom-utils.js iconUrl(), so an icon edit must
# move the fleet hash too (issue #372 — stale icons survived a deploy
# because SVG bytes never fed the hash). Everything else (manifest,
# the xterm vendor bundle) is cached more conservatively by the
# static-files mount itself.
_HASHED_SUFFIXES = (".js", ".css", ".svg")

# Subdirectories under static/ to skip entirely (vendor xterm is huge
# and immutable per upstream version — its URL never changes so it
# doesn't benefit from a content hash).
_SKIP_DIRS = ("vendor",)

# ``import ... from './foo.js'`` or ``'../dir/foo.js'`` — captures the
# whole quoted relative specifier (any number of ``./``/``../`` segments
# and subdirectories) so ``?v=<hash>`` can be stamped onto it. Any
# existing ``?v=…`` is captured too, so re-stamping an already-stamped
# body is idempotent.
_JS_IMPORT_RE = re.compile(
    r"""(from\s*['"])(\.\.?/(?:[\w\-.]+/)*[\w\-.]+\.js)(\?v=[^'"]*)?(['"])"""
)

# ``href``/``src`` pointing at a hashable ``/static/`` asset — including
# subdirectories (e.g. ``/static/_vendored/nav/nav-tabs.css``). Same
# idempotence rule as the JS import regex. ``/static/vendor/…`` paths
# still pass through unstamped because ``vendor`` never appears in the
# hash map (``_SKIP_DIRS``), not because the regex excludes them.
_INDEX_ASSET_RE = re.compile(
    r"""(href|src)=(['"])/static/([\w\-./]+\.(?:css|js))(\?v=[^'"]*)?(['"])"""
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
    """Return ``{relpath: fleet_hash}`` for every hashable static file.

    Every value is the same fleet hash (see the module docstring); the
    dict is keyed by the file's static-dir-relative posix path (e.g.
    ``_vendored/nav/nav-tabs.css``), not the bare filename, so the
    rewriters can resolve subdirectory references and so two files
    sharing a basename in different directories don't collide.
    """
    if not static_dir.exists():
        return {}
    per_file: Dict[str, str] = {}
    for path in _iter_hashable_files(static_dir):
        relpath = path.relative_to(static_dir).as_posix()
        per_file[relpath] = _short_hash(path.read_bytes())
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


def _resolve_specifier(from_dir: str, spec: str) -> str:
    """Resolve a ``./``/``../`` import specifier against ``from_dir``.

    ``from_dir`` is the static-dir-relative posix directory of the file
    doing the importing (empty string at the static root). Returns the
    static-dir-relative posix path used as the ``hashes`` lookup key,
    e.g. ``_resolve_specifier("_vendored/empty-state", "../icons/icons.js")
    == "_vendored/icons/icons.js"``.
    """
    joined = posixpath.join(from_dir, spec) if from_dir else spec
    return posixpath.normpath(joined)


def rewrite_js_imports(body: str, hashes: Dict[str, str], from_dir: str = "") -> str:
    """Stamp ``?v=<hash>`` onto every relative ``import`` in ``body``.

    ``from_dir`` is the static-dir-relative posix directory of the file
    being rewritten (empty string for a file at the static root) —
    needed to resolve ``./`` and ``../`` specifiers (including into
    subdirectories, e.g. ``./_vendored/icons/icons.js``) against
    ``hashes``, which is keyed by static-dir-relative path. Imports with
    no matching entry are left alone. Existing ``?v=…`` is replaced, so
    re-rewriting a served body is idempotent.
    """
    if not hashes:
        return body

    def _sub(match: re.Match) -> str:
        prefix, spec, _existing, quote_close = match.group(1, 2, 3, 4)
        stamp = hashes.get(_resolve_specifier(from_dir, spec))
        if not stamp:
            return match.group(0)
        return f"{prefix}{spec}?v={stamp}{quote_close}"

    return _JS_IMPORT_RE.sub(_sub, body)


def rewrite_index_html(body: str, hashes: Dict[str, str]) -> str:
    """Stamp ``?v=<hash>`` onto every ``/static/<relpath>.(css|js)`` href/src.

    ``<relpath>`` may include subdirectories (e.g.
    ``_vendored/nav/nav-tabs.css``) since it maps directly onto a
    ``hashes`` key — no resolution needed, unlike a relative JS import.
    Unknown files pass through unchanged — robust against a new asset
    not yet in the hash map. Existing ``?v=…`` is replaced.
    """
    if not hashes:
        return body

    def _sub(match: re.Match) -> str:
        attr, quote_open, relpath, _existing, quote_close = match.group(1, 2, 3, 4, 5)
        stamp = hashes.get(relpath)
        if not stamp:
            return match.group(0)
        return f'{attr}={quote_open}/static/{relpath}?v={stamp}{quote_close}'

    return _INDEX_ASSET_RE.sub(_sub, body)


def asset_hash_for(hashes: Dict[str, str], name: str) -> Optional[str]:
    """Lookup helper that survives an empty map without raising."""
    if not hashes:
        return None
    return hashes.get(name)
