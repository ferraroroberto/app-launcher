"""Generate ``*-remote.bat`` files for Claude Code projects.

Sync rules:
- Every ``.code-workspace`` maps to a ``{name}-remote.bat`` in the same
  directory. New bats are always created; existing bats are only
  overwritten when explicitly opted in.
- Every orphan ``*-remote.bat`` (no matching workspace) can optionally
  have a minimal workspace generated for it.

Lifted from the original `launcher.py` `_render_*` helpers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .scanner import _read_remote_bat_project_dir, _read_workspace_project_dir

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceRow:
    name: str
    project_dir: Path
    bat_name: str
    bat_exists: bool


@dataclass
class OrphanBatRow:
    name: str
    project_dir: Path
    bat_name: str
    ws_name: str


@dataclass
class GenerateResult:
    created: List[str] = field(default_factory=list)
    overwritten: List[str] = field(default_factory=list)
    ws_created: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def discover_workspaces(projects_dir: Path) -> List[WorkspaceRow]:
    if not projects_dir.is_dir():
        return []
    rows: List[WorkspaceRow] = []
    for ws in sorted(projects_dir.glob("*.code-workspace")):
        project_dir = _read_workspace_project_dir(ws, projects_dir)
        if project_dir is None:
            continue
        bat_name = ws.stem + "-remote.bat"
        rows.append(
            WorkspaceRow(
                name=ws.stem,
                project_dir=project_dir,
                bat_name=bat_name,
                bat_exists=(projects_dir / bat_name).exists(),
            )
        )
    return rows


def discover_orphan_bats(projects_dir: Path) -> List[OrphanBatRow]:
    if not projects_dir.is_dir():
        return []
    workspace_stems = {ws.stem for ws in projects_dir.glob("*.code-workspace")}
    rows: List[OrphanBatRow] = []
    for bat in sorted(projects_dir.glob("*-remote.bat")):
        stem = bat.stem[: -len("-remote")]
        if stem in workspace_stems:
            continue
        project_dir = _read_remote_bat_project_dir(bat) or (projects_dir / stem)
        rows.append(
            OrphanBatRow(
                name=stem,
                project_dir=project_dir,
                bat_name=bat.name,
                ws_name=stem + ".code-workspace",
            )
        )
    return rows


def render_bat_content(project_dir: Path, flags: str) -> str:
    d = str(project_dir)
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "\r\n"
        ":: -----------------------------------------------\r\n"
        ":: launch_claude_remote.bat\r\n"
        ":: Opens Claude Code with Remote Control enabled\r\n"
        ":: -----------------------------------------------\r\n"
        "\r\n"
        f'set "PROJECT_DIR={d}"\r\n'
        "\r\n"
        ":: -----------------------------------------------\r\n"
        "\r\n"
        'if not exist "%PROJECT_DIR%" (\r\n'
        "    echo [ERROR] Folder not found: %PROJECT_DIR%\r\n"
        "    pause\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "echo.\r\n"
        "echo  Starting Claude Code with Remote Control\r\n"
        "echo  Project: %PROJECT_DIR%\r\n"
        "echo.\r\n"
        "\r\n"
        'cd /d "%PROJECT_DIR%"\r\n'
        "\r\n"
        f'"C:\\Windows\\System32\\cmd.exe" /c claude {flags}\r\n'
        "\r\n"
        "endlocal\r\n"
    )


def render_workspace_content(project_dir: Path, projects_dir: Path) -> str:
    try:
        rel = project_dir.relative_to(projects_dir)
        path_str = str(rel).replace("\\", "/")
    except ValueError:
        path_str = str(project_dir)
    return json.dumps({"folders": [{"path": path_str}]}, indent="\t") + "\n"


def run_generate(
    *,
    projects_dir: Path,
    flags: str,
    overwrite_names: set[str],
    create_ws_names: set[str],
) -> GenerateResult:
    """Execute one generate pass. Mirrors the original ``/generate/run``."""
    result = GenerateResult()

    for ws in discover_workspaces(projects_dir):
        bat_path = projects_dir / ws.bat_name
        if ws.bat_exists and ws.name not in overwrite_names:
            continue
        try:
            bat_path.write_bytes(
                render_bat_content(ws.project_dir, flags).encode("utf-8")
            )
            (result.overwritten if ws.bat_exists else result.created).append(
                ws.bat_name
            )
            logger.info(f"✅ Wrote {ws.bat_name}")
        except OSError as exc:
            result.errors.append(f"{ws.bat_name}: {exc}")

    for orphan in discover_orphan_bats(projects_dir):
        if orphan.name not in create_ws_names:
            continue
        ws_path = projects_dir / orphan.ws_name
        try:
            ws_path.write_text(
                render_workspace_content(orphan.project_dir, projects_dir),
                encoding="utf-8",
            )
            result.ws_created.append(orphan.ws_name)
            logger.info(f"✅ Created workspace {orphan.ws_name}")
        except OSError as exc:
            result.errors.append(f"{orphan.ws_name}: {exc}")

    return result
