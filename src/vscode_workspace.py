"""Open a scanned Coding project in the local Visual Studio Code (issue #802).

The Coding tab's per-row VS Code button is deliberately *not* an agent launch:
there is no PTY, no session, and nothing for the launcher to track once the
editor is up. This module resolves the project's ``<name>.code-workspace``
file — which lives **next to** the project folder, directly under
``projects_dir``, not inside the project — creates it when it doesn't exist
yet, and hands it to the ``code`` CLI.

Kept out of :mod:`src.agents` on purpose: that registry is exclusively for
interactive terminal agents hosted by the session-host's ConPTY machinery
(quit command, resume token, fullscreen TUI flag), none of which describe a
GUI editor — and registering one there would need a session-host restart to
take effect, for no benefit.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Tuple

from src.env_path import effective_path
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

#: The CLI VS Code puts on ``PATH`` through its own "Add to PATH" setup step.
#: Genuinely absent when that step was skipped — which is why the Coding-tab
#: button carries an availability check at all.
VSCODE_COMMAND = "code"


def is_vscode_installed() -> bool:
    """Whether the ``code`` CLI resolves on the **effective** ``PATH``.

    Resolved exactly like :func:`src.agents.is_installed` — through
    :func:`src.env_path.effective_path`, not the inherited ``PATH`` — so a
    VS Code installed while the launcher is running lights the button up on
    the next detection poll rather than after an Explorer restart (#668).
    """
    return shutil.which(VSCODE_COMMAND, path=effective_path()) is not None


def workspace_path_for(projects_dir: Path, name: str) -> Path:
    """The ``.code-workspace`` file for the project folder ``name``.

    A *sibling* of the project directory, not a child: every workspace file
    on this machine sits directly under ``projects_dir`` (e.g.
    ``E:\\automation\\app-launcher.code-workspace``), with a ``folders[].path``
    that is the bare folder name relative to that same location.
    """
    return Path(projects_dir) / f"{name}.code-workspace"


def ensure_workspace_file(projects_dir: Path, name: str) -> Tuple[Path, bool]:
    """Return the workspace path, creating a minimal file when it's missing.

    Returns ``(path, created)``. An existing file is never rewritten — the
    user's own folder list, settings, and extension recommendations live in
    it. The generated shape matches the ones already on disk (tab-indented,
    one folder, bare relative name); the JSON is generated rather than copied
    from a template because every existing workspace names its own project,
    so there is no canonical file to copy from.
    """
    path = workspace_path_for(projects_dir, name)
    if path.exists():
        return path, False
    path.write_text(
        json.dumps({"folders": [{"path": name}]}, indent="\t") + "\n",
        encoding="utf-8",
    )
    logger.info(f"📝 created VS Code workspace: {path}")
    return path, True


def open_workspace(workspace_path: Path) -> int:
    """Spawn ``code <workspace_path>`` and return the spawned PID.

    ``code`` is a thin ``code.cmd`` shim that hands the path to an already
    running (or freshly started) VS Code and exits on its own — measured at
    ~1.6 s against a warm editor — so there is deliberately nothing here to
    keep alive, poll for a port, or track a window for, unlike an Apps-tab
    bat. ``NO_WINDOW`` keeps that shim's console off screen: the tray parent
    is windowless, so without it every tap would flash a CMD window on the PC.
    """
    resolved = shutil.which(VSCODE_COMMAND, path=effective_path())
    if resolved is None:
        raise OSError(f"{VSCODE_COMMAND!r} CLI not found on PATH")
    proc = subprocess.Popen(
        [resolved, str(workspace_path)],
        shell=False,
        creationflags=NO_WINDOW,
        close_fds=True,
    )
    logger.info(f"🚀 opened VS Code: {workspace_path} (pid {proc.pid})")
    return proc.pid
