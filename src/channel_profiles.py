"""Telegram channel launch profiles — ``config/channel_profiles.json`` (#1366).

A *profile* is one Telegram bot bound to one life-os skill: ``health`` opens the
Health skill with its own bot, ``school`` opens School with another. Each
profile owns a plugin **state directory** (the official Telegram channel
plugin's ``TELEGRAM_STATE_DIR``), which is where the bot token
(``<state dir>/.env``) and the sender allowlist (``access.json``) live.

The launcher never reads, stores or logs the token: a profile carries only the
directory's *path*. One bot token allows one ``getUpdates`` consumer, so every
profile needs its own bot and its own state directory, and a profile must never
run in two sessions at once — :func:`profile_session_label` is the tag the
launch route uses to find (and refuse) a second one.

The state directory reaches only that session: :func:`write_channel_settings`
writes a tiny Claude settings file whose ``env`` block carries the variable, and
the launch passes it with ``--settings`` (see
:func:`src.launch_flags.build_claude_flags`). Claude applies a settings ``env``
block to its own process, and the plugin's server is that process's child. No
session-host change is involved.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from src.runtime_data import runtime_data_dir

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES_PATH = PROJECT_ROOT / "config" / "channel_profiles.json"

# The session-host tags a session with this label (``telegram:<profile id>``);
# the Board, the Coding tab and the one-live-session-per-profile guard key on it.
LABEL_PREFIX = "telegram:"

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# The settings-file path travels inside the launch flags, which the session-host
# re-validates against an allowlist and tokenizes on whitespace — so refuse a
# location that would not survive that, rather than launch a broken command.
_SETTINGS_PATH_RE = re.compile(r"^[A-Za-z0-9_.:/\-]+$")


@dataclass(frozen=True)
class ChannelProfile:
    id: str
    label: str
    skill: str
    state_dir: str


def profile_session_label(profile_id: str) -> str:
    """The session-host ``label`` a profile's session carries."""
    return f"{LABEL_PREFIX}{profile_id}"


def label_profile_id(label: Optional[str]) -> str:
    """The profile id inside a session label, or ``""`` for any other label."""
    text = str(label or "")
    return text[len(LABEL_PREFIX):] if text.startswith(LABEL_PREFIX) else ""


def load_channel_profiles(
    path: Optional[Path] = None,
) -> Tuple[List[ChannelProfile], List[str]]:
    """Read the profile file into ``(profiles, problems)``.

    A missing file is not an error (no profiles, no problems): the feature is
    opt-in. An unreadable or malformed file, and each bad row, is reported in
    ``problems`` — never raised — so the Life OS tab can say what is wrong
    while the launcher keeps booting. The problem text names the profile id but
    never a path's contents.
    """
    target = Path(path) if path is not None else DEFAULT_PROFILES_PATH
    if not target.exists():
        return [], []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"⚠️ Could not read {target.name} ({exc})")
        return [], [f"{target.name} is unreadable: {exc}"]
    rows = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return [], [f'{target.name} needs a top-level "profiles" list']

    profiles: List[ChannelProfile] = []
    problems: List[str] = []
    seen_ids: set[str] = set()
    seen_dirs: dict[str, str] = {}
    for index, row in enumerate(rows):
        pid = str(row.get("id") or "").strip() if isinstance(row, dict) else ""
        where = f"profile {pid!r}" if pid else f"profile #{index + 1}"
        if not isinstance(row, dict) or not _ID_RE.match(pid):
            problems.append(f"{where}: id must be lowercase letters, digits, hyphens")
            continue
        if pid in seen_ids:
            problems.append(f"{where}: duplicate id")
            continue
        seen_ids.add(pid)
        skill = _text(row.get("skill"))
        state_dir = _text(row.get("state_dir"))
        if not skill:
            problems.append(f"{where}: skill is required")
            continue
        if not state_dir or not Path(state_dir).is_absolute():
            problems.append(f"{where}: state_dir must be an absolute path")
            continue
        if not Path(state_dir).is_dir():
            problems.append(
                f"{where}: state_dir does not exist — create it and configure "
                "the bot there first"
            )
            continue
        # Two profiles on one directory would share a token: the 409 the
        # one-bot-per-profile rule exists to prevent.
        key = str(Path(state_dir).resolve()).lower()
        if key in seen_dirs:
            problems.append(f"{where}: state_dir is already used by {seen_dirs[key]!r}")
            continue
        seen_dirs[key] = pid
        profiles.append(
            ChannelProfile(
                id=pid,
                label=_text(row.get("label")) or pid.replace("-", " ").title(),
                skill=skill,
                state_dir=state_dir,
            )
        )
    return profiles, problems


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def write_channel_settings(profile: ChannelProfile) -> str:
    """Write the profile's per-session settings file; return its flag-safe path.

    The file holds only the state directory's path (a settings ``env`` block),
    never a token. It lives under the app's runtime-data directory and is
    rewritten on every launch, so a profile edit takes effect on the next start.
    """
    directory = runtime_data_dir("app-launcher", create=True) / "channel-settings"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{profile.id}.json"
    payload = {"env": {"TELEGRAM_STATE_DIR": Path(profile.state_dir).as_posix()}}
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    flag_path = target.resolve().as_posix()
    if not _SETTINGS_PATH_RE.match(flag_path):
        raise ValueError(
            "the runtime-data directory path has characters that cannot ride a "
            "launch command (spaces or special characters); point "
            "APP_LAUNCHER_DATA_DIR at a plain path"
        )
    return flag_path


# ------------------------------------------------------------ setup checks
PLUGIN_ID = "telegram@claude-plugins-official"


def profiles_file_present(path: Optional[Path] = None) -> bool:
    """Whether the profile file exists (the feature's opt-in switch)."""
    return (Path(path) if path is not None else DEFAULT_PROFILES_PATH).is_file()


def env_file_present(profile: ChannelProfile) -> bool:
    """Whether the profile's ``<state dir>/.env`` exists.

    A stat, never an open: the file holds the bot token, which the launcher
    must not read. Presence says the owner did step 3 of the setup, nothing
    about whether the token is valid.
    """
    return (Path(profile.state_dir) / ".env").is_file()


def bun_available() -> bool:
    """Whether Bun (the plugin server's runtime) is on ``PATH`` or in its
    default install directory."""
    if shutil.which("bun"):
        return True
    return (Path.home() / ".bun" / "bin" / "bun.exe").is_file() or (
        Path.home() / ".bun" / "bin" / "bun"
    ).is_file()


def plugin_installed_for(
    life_os_dir: Path, installed_plugins: Optional[Path] = None
) -> Optional[bool]:
    """Whether the Telegram channel plugin is installed for the life-os project.

    ``True`` for a user-scope install or a project-scope one whose
    ``projectPath`` is ``life_os_dir``; ``False`` when it is absent or
    installed for other projects only; ``None`` (unknown) when Claude's install
    record cannot be read — never folded into ``False`` or ``True``.
    """
    record = installed_plugins or (
        Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    )
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        entries = data["plugins"].get(PLUGIN_ID, [])
    except (OSError, ValueError, KeyError, AttributeError):
        return None
    if not isinstance(entries, list):
        return None
    target = str(Path(life_os_dir).resolve()).lower()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("scope") == "user":
            return True
        project = entry.get("projectPath")
        if isinstance(project, str) and str(Path(project).resolve()).lower() == target:
            return True
    return False
