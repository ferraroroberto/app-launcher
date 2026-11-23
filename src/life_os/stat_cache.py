"""A directory listing kept in memory and re-read only when a folder's mtime moves.

A request that lists a folder used to walk the disk on every call. A listing
only changes when an entry is added, removed or renamed, and that moves the
containing folder's mtime, so :class:`DirListingCache` keeps the last answer
together with the mtime of every folder it was built from. A read that finds
those mtimes unchanged costs one ``stat`` per folder and no directory read.

The fingerprint is taken *before* the build: a change that lands while the
build runs leaves the stored fingerprint older than the folder, so the next
read rebuilds instead of trusting a half-seen listing.

A folder's mtime has coarse resolution (one timer tick on NTFS, 2 s on FAT),
so a second change in the same tick as the one before the build leaves the
mtime where it was. A fingerprint holding an mtime that recent is therefore
not stored, and reads rebuild until the folder has been quiet for the window.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Hashable, List, Optional, Tuple, TypeVar

T = TypeVar("T")

# folder path -> its st_mtime_ns; a folder that cannot be statted records None.
_Fingerprint = Tuple[Tuple[str, Optional[int]], ...]

# Wider than any filesystem's mtime resolution (FAT's 2 s is the coarsest), so
# an mtime older than this before the build cannot hide a later change.
_RACY_WINDOW_NS = 2_000_000_000


def _mtime_ns(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _folders_under(root: Path, recursive: bool, skip: FrozenSet[str]) -> List[str]:
    """``root`` and, when ``recursive``, every folder below it that is not named in ``skip``."""
    if not recursive:
        return [str(root)]
    folders: List[str] = []
    for dirpath, dirs, _files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        folders.append(dirpath)
    return folders or [str(root)]


def _settled(fingerprint: _Fingerprint, taken_ns: int) -> bool:
    """True when every folder's mtime is older than the racy window before ``taken_ns``."""
    return all(mtime is None or mtime < taken_ns - _RACY_WINDOW_NS for _folder, mtime in fingerprint)


class DirListingCache:
    """Maps ``(root, key)`` to a built listing, valid while its folders' mtimes hold."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, Hashable], Tuple[_Fingerprint, object]] = {}

    def get(
        self,
        root: Path,
        key: Hashable,
        build: Callable[[], T],
        *,
        recursive: bool = False,
        skip: FrozenSet[str] = frozenset(),
    ) -> T:
        """The listing for ``root``: the kept one while unchanged, else ``build()``'s fresh answer.

        ``key`` tells apart different listings of the same folder (a different
        category, a different glob). ``recursive`` says the listing reads the
        whole tree below ``root``, so every folder in it is part of the
        fingerprint, except folders named in ``skip`` (the listing ignores
        them). The caller must treat the returned value as read-only.
        """
        slot = (str(root), key)
        with self._lock:
            held = self._entries.get(slot)
        if held is not None and self._current(held[0]):
            return held[1]  # type: ignore[return-value]
        folders = _folders_under(root, recursive, skip)
        taken_ns = time.time_ns()
        fingerprint = tuple((folder, _mtime_ns(folder)) for folder in folders)
        value = build()
        if _settled(fingerprint, taken_ns):
            with self._lock:
                self._entries[slot] = (fingerprint, value)
        else:
            with self._lock:
                self._entries.pop(slot, None)
        return value

    @staticmethod
    def _current(fingerprint: _Fingerprint) -> bool:
        return all(_mtime_ns(folder) == mtime for folder, mtime in fingerprint)
