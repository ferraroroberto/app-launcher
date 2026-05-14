"""Filesystem scanner for the unified app registry.

Two pieces of discovery share this module:

- ``scan_claude_code_projects(projects_dir)`` — looks at the **direct
  children** of ``projects_dir`` for ``*.code-workspace`` files and
  orphan ``*-remote.bat`` files. Each becomes a ``claude-code`` row.

- ``scan_app_bats(scan_root)`` — walks ``scan_root`` recursively
  looking at every ``*.bat``, classifies via ``classify_bat``, and
  returns ``(path, kind)`` pairs for kinds ``streamlit``, ``webapp``,
  ``tunnel``.

The two scans run independently — a Claude Code project never collides
with an Apps row because Claude Code rows have no ``bat_path`` (the
launcher generates the bat content on the fly).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

APPS_SCAN_SKIP_DIRS = frozenset(
    {".venv", "venv", "__pycache__", "node_modules", "certificates", ".git", "old"}
)

# kind constants — used as string literals everywhere else.
KIND_CLAUDE_CODE = "claude-code"
KIND_STREAMLIT = "streamlit"
KIND_WEBAPP = "webapp"
KIND_TUNNEL = "tunnel"

VALID_KINDS = frozenset({KIND_CLAUDE_CODE, KIND_STREAMLIT, KIND_WEBAPP, KIND_TUNNEL})


@dataclass(frozen=True)
class ClaudeCodeProject:
    """A discovered project that the Claude Code tab can launch.

    ``project_dir`` is where ``claude`` will be cwd'd into. ``source``
    is either ``"workspace"`` (a ``.code-workspace`` exists) or
    ``"orphan_bat"`` (only a ``*-remote.bat`` exists).
    """

    id: str
    name: str
    project_dir: Path
    source: str  # "workspace" | "orphan_bat"


# ----------------------------------------------------------- pretty names


def pretty_name_from_stem(stem: str) -> str:
    """Turn ``client_x-remote`` into ``Client X``."""
    parts = [p for p in re.split(r"[_\-\s]+", stem) if p and p.lower() != "remote"]
    if not parts:
        parts = [stem]
    return " ".join(p.capitalize() for p in parts)


def pretty_folder_name(folder: Path) -> str:
    parts = [p for p in re.split(r"[_\-\s]+", folder.name) if p]
    if not parts:
        parts = [folder.name]
    return " ".join(p.capitalize() for p in parts)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return cleaned or "app"


# ----------------------------------------------------------- claude code


def scan_claude_code_projects(projects_dir: Path) -> List[ClaudeCodeProject]:
    """Discover ``.code-workspace`` files and orphan ``*-remote.bat`` files."""
    if not projects_dir.is_dir():
        logger.warning(f"⚠️ Projects dir does not exist: {projects_dir}")
        return []

    results: List[ClaudeCodeProject] = []
    workspace_stems: set[str] = set()

    for ws in sorted(projects_dir.glob("*.code-workspace")):
        project_dir = _read_workspace_project_dir(ws, projects_dir)
        if project_dir is None:
            continue
        results.append(
            ClaudeCodeProject(
                id=ws.stem,
                name=pretty_name_from_stem(ws.stem),
                project_dir=project_dir,
                source="workspace",
            )
        )
        workspace_stems.add(ws.stem)

    for bat in sorted(projects_dir.glob("*-remote.bat")):
        stem = bat.stem[: -len("-remote")]
        if stem in workspace_stems:
            continue
        project_dir = _read_remote_bat_project_dir(bat) or (projects_dir / stem)
        results.append(
            ClaudeCodeProject(
                id=stem,
                name=pretty_name_from_stem(stem),
                project_dir=project_dir,
                source="orphan_bat",
            )
        )

    results.sort(key=lambda x: x.name.lower())
    return results


def _read_workspace_project_dir(ws: Path, projects_dir: Path) -> Optional[Path]:
    try:
        data = json.loads(ws.read_text(encoding="utf-8"))
        raw_path = data["folders"][0]["path"]
    except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        logger.debug(f"workspace parse failed for {ws}: {exc}")
        return None
    project_dir = Path(raw_path)
    if not project_dir.is_absolute():
        project_dir = (projects_dir / raw_path).resolve()
    return project_dir


def _read_remote_bat_project_dir(bat: Path) -> Optional[Path]:
    try:
        for line in bat.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r'set\s+"PROJECT_DIR=(.+)"', line.strip())
            if m:
                return Path(m.group(1).strip())
    except OSError as exc:
        logger.debug(f"orphan-bat parse failed for {bat}: {exc}")
    return None


# ----------------------------------------------------------- apps (bats)


def classify_bat(bat_path: Path) -> Optional[str]:
    """Return ``"streamlit"`` | ``"webapp"`` | ``"tunnel"`` | ``None``.

    Classification is mutually exclusive — the first match wins:

    * ``streamlit`` — body contains ``streamlit run``. Bats that *also*
      embed ``cloudflared tunnel`` inline (e.g. hybrid ``launch_server.bat``)
      stay in this bucket; they don't write a URL file we can surface.
    * ``tunnel`` — filename stem contains ``tunnel`` AND body references
      ``uvicorn`` / ``run_tunnel`` / ``cloudflared``. These are the
      only bats we surface a tunnel URL for.
    * ``webapp`` — body runs ``uvicorn`` (or imports ``app.webapp.server``).
    """
    try:
        text = bat_path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return None
    if "streamlit run" in text:
        return KIND_STREAMLIT
    stem = bat_path.stem.lower()
    has_tunnel_signal = any(
        token in text for token in ("uvicorn", "run_tunnel", "cloudflared")
    )
    if "tunnel" in stem and has_tunnel_signal:
        return KIND_TUNNEL
    if (
        "uvicorn" in text
        or "app.webapp.server" in text
        or "app/webapp/server" in text
    ):
        return KIND_WEBAPP
    return None


def scan_app_bats(scan_root: Path) -> List[Tuple[Path, str]]:
    """Recursively scan ``scan_root``, returning ``(path, kind)`` pairs.

    Skips ``APPS_SCAN_SKIP_DIRS`` and unclassifiable bats.
    """
    if not scan_root.is_dir():
        logger.warning(f"⚠️ Apps scan root does not exist: {scan_root}")
        return []

    found: List[Tuple[Path, str]] = []
    for bat in scan_root.rglob("*.bat"):
        if any(part in APPS_SCAN_SKIP_DIRS for part in bat.parts):
            continue
        kind = classify_bat(bat)
        if kind is not None:
            found.append((bat, kind))
    found.sort(key=lambda pair: pair[0])
    return found


def app_id_from_path(bat_path: Path, scan_root: Path) -> str:
    """Stable id derived from the bat's path relative to ``scan_root``."""
    try:
        rel = bat_path.resolve().relative_to(scan_root)
    except ValueError:
        rel = Path(bat_path.name)
    return slugify(str(rel.with_suffix("")))


def tunnel_url_for(bat_path: Path) -> Optional[str]:
    """Resolve a tunnel app's public URL.

    Prefers ``<bat.parent>/webapp/last_tunnel_url.txt`` — written at
    runtime, and includes the app's ``?token=`` when it has one. Falls
    back to the ingress hostname statically configured in
    ``<bat.parent>/webapp/cloudflared.yml``, so a sibling whose tray
    never writes the URL file (older template versions) still surfaces
    its named-tunnel URL.
    """
    webapp_dir = bat_path.parent / "webapp"
    try:
        text = (webapp_dir / "last_tunnel_url.txt").read_text(
            encoding="utf-8"
        ).strip()
        if text:
            return text
    except (OSError, UnicodeDecodeError):
        pass
    return _tunnel_url_from_cloudflared_yml(webapp_dir / "cloudflared.yml")


def _tunnel_url_from_cloudflared_yml(config_path: Path) -> Optional[str]:
    """First ``ingress[].hostname`` in a cloudflared config → ``https://<host>``."""
    if not config_path.is_file():
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        logger.debug(f"cloudflared.yml parse failed for {config_path}: {exc}")
        return None
    for entry in data.get("ingress") or []:
        if isinstance(entry, dict) and entry.get("hostname"):
            return f"https://{str(entry['hostname']).strip()}"
    return None
