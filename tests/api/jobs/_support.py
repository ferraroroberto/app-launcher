"""Shared helpers for the /api/jobs tests: real stub scripts and a seeded job."""

from __future__ import annotations

import tempfile
from pathlib import Path



# Save-time pre-flight (issue #69) rejects a non-existent script_path with
# a 400 before the row is ever persisted, so the happy-path tests need
# *real* files on disk. A single session temp dir holds throwaway stubs;
# _stub_path(name) lazily creates one and returns its absolute path.
_STUB_DIR: Path | None = None


def _stub_path(name: str = "demo.bat") -> str:
    """A real stub script under a venv-complete temp dir.

    The dir carries a ``.venv\\Scripts\\python.exe`` marker so a ``.py``
    stub resolves its interpreter cleanly (no venv-fallback warning) —
    the happy-path tests want a clean save. The dedicated venv-warning
    test uses :func:`_stub_path_no_venv` instead.
    """
    global _STUB_DIR
    if _STUB_DIR is None:
        _STUB_DIR = Path(tempfile.mkdtemp(prefix="al-jobs-stub-"))
        venv_py = _STUB_DIR / ".venv" / "Scripts" / "python.exe"
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.write_text("", encoding="utf-8")
    p = _STUB_DIR / name
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "@echo off\n" if name.lower().endswith(".bat") else "print('ok')\n"
        p.write_text(body, encoding="utf-8")
    return str(p)


def _stub_path_no_venv(name: str = "novenv.py") -> str:
    """A real ``.py`` stub in a fresh temp dir with NO ``.venv`` ancestor.

    Used to exercise the pre-flight venv-fallback warning (issue #69).
    """
    root = Path(tempfile.mkdtemp(prefix="al-jobs-novenv-"))
    p = root / name
    p.write_text("print('ok')\n", encoding="utf-8")
    return str(p)


def _seed_one_job(client, name="Demo", script=None,
                  schedule=None, args=""):
    payload = {
        "name": name,
        "script_path": script or _stub_path("demo.bat"),
        "args": args,
        "schedule": schedule or {"type": "none"},
    }
    return client.post("/api/jobs", json=payload)
