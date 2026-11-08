"""Writers for runtime files whose path the runtime-write ratchet cannot resolve.

The launcher's state lives where a parameter, a config value or an environment
override says (``webapp/``, ``config/``, the machine runtime root, a project
folder the request names). The ratchet cannot follow such a path statically, so
every write would need its own exempt marker. These writers take the path,
refuse it at runtime when it resolves under one of the code folders the ratchet
guards, and then write. The guarantee a marker only claimed is enforced here.

Stdlib-only and import-free of the rest of the repo, so any module can use it.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import IO, Any, Union

PathLike = Union[str, "os.PathLike[str]"]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The folders `[runtime_write_ratchet] code_folders` guards (its default set).
CODE_FOLDERS = ("src", "app", "scripts", "hooks", "skills")


def checked(path: PathLike) -> Path:
    """``path`` resolved, or ``ValueError`` when it sits under a code folder."""
    resolved = Path(path).expanduser().resolve()
    for name in CODE_FOLDERS:
        folder = REPO_ROOT / name
        if resolved == folder or folder in resolved.parents:
            raise ValueError(
                f"Refusing to write under the code folder {folder}: {resolved}. "
                "Runtime output belongs in webapp/, config/ or the runtime data root."
            )
    return resolved


def make_dirs(path: PathLike, *, parents: bool = True, exist_ok: bool = True) -> Path:
    """Create the folder ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named folder; checked() above raises when it is under a code folder
    target.mkdir(parents=parents, exist_ok=exist_ok)
    return target


def open_for_write(path: PathLike, mode: str = "w", **kwargs: Any) -> IO[Any]:
    """Open ``path`` with ``mode`` (creating no folders); refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named file; checked() above raises when it is under a code folder
    return target.open(mode, **kwargs)


def save_text(path: PathLike, text: str, encoding: str = "utf-8", **kwargs: Any) -> None:
    """Write ``text`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "w", encoding=encoding, **kwargs) as handle:
        handle.write(text)


def save_bytes(path: PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "wb") as handle:
        handle.write(data)


def move(src: PathLike, dst: PathLike) -> Path:
    """Move ``src`` to ``dst``; refuses a destination under a code folder."""
    target = checked(dst)
    # runtime-write-exempt: caller-named destination; checked() above raises when it is under a code folder
    shutil.move(str(src), str(target))
    return target


def replace(src: PathLike, dst: PathLike) -> Path:
    """Atomically swap ``src`` over ``dst``; refuses a destination under a code folder."""
    target = checked(dst)
    os.replace(src, target)
    return target


def file_handler(path: PathLike, mode: str = "a", encoding: str = "utf-8") -> logging.FileHandler:
    """A ``logging.FileHandler`` on ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named log file; checked() above raises when it is under a code folder
    return logging.FileHandler(target, mode=mode, encoding=encoding)


def connect_sqlite(path: PathLike, **kwargs: Any) -> sqlite3.Connection:
    """Open the SQLite file ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named database; checked() above raises when it is under a code folder
    return sqlite3.connect(target, **kwargs)


def connect_sqlite_read_only(path: PathLike) -> sqlite3.Connection:
    """Open the existing SQLite file ``path`` with ``mode=ro``."""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    # runtime-write-exempt: a read-only connection (mode=ro); nothing is written
    return sqlite3.connect(uri, uri=True)
