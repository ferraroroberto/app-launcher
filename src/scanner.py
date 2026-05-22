"""Filesystem scanner for the unified app registry.

Two pieces of discovery share this module:

- ``scan_project_dirs(projects_dir, ignore)`` — lists the **direct child
  directories** of ``projects_dir``, dropping VCS / build noise and any
  directory whose name matches a gitignore-style ignore pattern. Each
  surviving directory becomes a ``claude-code`` row. There is no scan
  step and no on-disk marker file — the directory listing is the source
  of truth, recomputed live on every request.

- ``scan_app_bats(scan_root)`` — walks ``scan_root`` recursively
  looking at every ``*.bat``, classifies via ``classify_bat``, and
  returns ``(path, kind)`` pairs for kinds ``streamlit``, ``webapp``,
  ``tunnel``.

The two scans run independently — a Claude Code project never collides
with an Apps row because Claude Code rows have no ``bat_path`` (the
launcher launches ``claude`` in the directory directly).
"""

from __future__ import annotations

import configparser
import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import yaml

logger = logging.getLogger(__name__)

APPS_SCAN_SKIP_DIRS = frozenset(
    {".venv", "venv", "__pycache__", "node_modules", "certificates", ".git", "old"}
)

# Directories never offered as Claude Code projects, regardless of the
# user's ignore list — VCS metadata, virtualenvs, build caches, IDE dirs.
PROJECT_SCAN_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".idea", ".vscode"}
)

# kind constants — used as string literals everywhere else.
KIND_CLAUDE_CODE = "claude-code"
KIND_STREAMLIT = "streamlit"
KIND_WEBAPP = "webapp"
KIND_TUNNEL = "tunnel"

VALID_KINDS = frozenset({KIND_CLAUDE_CODE, KIND_STREAMLIT, KIND_WEBAPP, KIND_TUNNEL})


@dataclass(frozen=True)
class ProjectDir:
    """A project directory the Coding tab can launch a coding agent in.

    ``project_dir`` is the directory the agent will be cwd'd into; ``id``
    is a stable slug of its name; ``name`` is the **bare on-disk folder
    name**, shown verbatim on the tile (no prettification — that's the
    Coding-tab tile design from issue #45).
    """

    id: str
    name: str
    project_dir: Path


# ----------------------------------------------------------- pretty names


def pretty_folder_name(folder: Path) -> str:
    parts = [p for p in re.split(r"[_\-\s]+", folder.name) if p]
    if not parts:
        parts = [folder.name]
    return " ".join(p.capitalize() for p in parts)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return cleaned or "app"


# ----------------------------------------------------------- claude code


def dir_ignored(name: str, patterns: Sequence[str]) -> bool:
    """Return ``True`` when directory ``name`` matches any ignore pattern.

    Patterns are gitignore-style and matched case-insensitively against
    the bare directory name: a plain entry matches by name, ``*`` / ``?``
    globs are honoured (e.g. ``*-old`` or ``tmp?``). Since the scan only
    ever looks one level deep, slashes carry no extra meaning.
    """
    lowered = name.lower()
    for pattern in patterns:
        pat = str(pattern).strip().lower()
        if pat and fnmatch.fnmatch(lowered, pat):
            return True
    return False


def scan_project_dirs(
    projects_dir: Path, ignore: Optional[Sequence[str]] = None
) -> List[ProjectDir]:
    """List direct child directories of ``projects_dir`` as launchable rows.

    Always-skips :data:`PROJECT_SCAN_SKIP_DIRS`; additionally drops any
    directory whose name matches an entry in ``ignore`` (see
    :func:`dir_ignored`). Results are sorted by name, case-insensitively.
    """
    if not projects_dir.is_dir():
        logger.warning(f"⚠️ Projects dir does not exist: {projects_dir}")
        return []

    patterns = list(ignore or [])
    results: List[ProjectDir] = []
    for child in projects_dir.iterdir():
        try:
            if not child.is_dir():
                continue
        except OSError:  # broken junction / permission error
            continue
        if child.name in PROJECT_SCAN_SKIP_DIRS:
            continue
        if dir_ignored(child.name, patterns):
            continue
        results.append(
            ProjectDir(
                id=slugify(child.name),
                name=child.name,
                project_dir=child,
            )
        )
    results.sort(key=lambda p: p.name.lower())
    return results


# ----------------------------------------------------------- github repo


def _normalise_github_url(url: str) -> Optional[str]:
    """Turn a git remote URL into a browsable GitHub repo URL, or ``None``.

    Handles the three common remote forms — SCP-style SSH
    (``git@github.com:owner/repo.git``), HTTPS
    (``https://github.com/owner/repo.git``), and the explicit
    ``ssh://git@github.com/owner/repo`` form. Any non-GitHub host yields
    ``None``. A trailing ``.git`` and surrounding slashes are stripped.
    """
    scp = re.match(r"git@github\.com:(.+)", url, re.IGNORECASE)
    if scp:
        path = scp.group(1)
    else:
        proto = re.match(
            r"(?:https?|ssh|git)://(?:[^@/]+@)?github\.com/(.+)",
            url,
            re.IGNORECASE,
        )
        if not proto:
            return None
        path = proto.group(1)

    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    return f"https://github.com/{path}" if path else None


def github_repo_url(project_dir: Path) -> Optional[str]:
    """Resolve the GitHub repo URL for a project from its ``origin`` remote.

    Reads ``<project_dir>/.git/config`` directly — no ``git`` subprocess
    — and normalises the ``origin`` remote URL via
    :func:`_normalise_github_url`. Returns ``None`` when the folder has
    no ``.git/config``, no ``origin`` remote, or a non-GitHub remote.
    """
    config_path = project_dir / ".git" / "config"
    if not config_path.is_file():
        return None

    # strict=False: git's config format allows a key to repeat within a
    # section (multivar), and tools like VS Code do write duplicates
    # (e.g. vscode-merge-base) — configparser's default strict mode
    # rejects those. We only read remote.origin.url, where last wins.
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        logger.warning(f"⚠️  Could not read {config_path} ({exc})")
        return None

    raw = parser.get('remote "origin"', "url", fallback=None)
    if not raw:
        return None
    return _normalise_github_url(raw.strip())


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
