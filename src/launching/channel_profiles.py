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
:func:`src.launching.flags.build_claude_flags`). Claude applies a settings ``env``
block to its own process, and the plugin's server is that process's child. No
session-host change is involved.

**Relay (#1639), opt-in per profile.** A profile with ``"relay": true`` lets its
bot find, resume and steer the skill's *other* conversations through the
launcher's relay routes (:mod:`app.webapp.routers.life_os_relay`) and read the
reply back, while the bot stays the only Telegram reader. Its settings file then
also carries a fresh random relay token (the routes map the token to this
profile: identity never comes from a caller-supplied skill name), the relay's
loopback URL and command, and a ``permissions.allow`` rule for exactly that
command. The same file turns the fleet context filter off for that one session:
its PreToolUse rewrite re-encodes every Bash command, so the allow rule never
matches and the auto-mode classifier decides instead (the read-back refusal
#1639's Probe 2 hit). Without the flag the file is exactly as before.

**Router commands.** A profile running the ``general`` skill suggests which skill
and conversation fit a message by running two read-only commands of the skill's
own script. Its settings file allows exactly those two shapes
(:func:`router_allow_rules`) and, like relay, turns the context filter off so
the rules match; every other profile's file is unchanged.

**Editing.** :func:`update_channel_profile` is the only writer of the profile
file, and it changes only :data:`EDITABLE_FIELDS`. The fields a running bot reads
once, at its start (:data:`RESTART_FIELDS`), are recorded by every launch so
:func:`restart_fields` can tell the app a restart would change something.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional, Tuple

from src.launching.model_catalog import CLAUDE_MODEL_SPECS, available_values
from src.runtime_data import runtime_data_dir

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILES_PATH = PROJECT_ROOT / "config" / "channel_profiles.json"

# The session-host tags a session with this label (``telegram:<profile id>``);
# the Board, the Coding tab and the one-live-session-per-profile guard key on it.
LABEL_PREFIX = "telegram:"
# A conversation the relay opened for a profile carries this label, so the Board
# does not read it as the owner's own interactive session.
RELAY_LABEL_PREFIX = "relay:"

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# The settings-file path travels inside the launch flags, which the session-host
# re-validates against an allowlist and tokenizes on whitespace — so refuse a
# location that would not survive that, rather than launch a broken command.
_SETTINGS_PATH_RE = re.compile(r"^[A-Za-z0-9_.:/\-]+$")

# Idle close: minutes without activity before a relay-reopened conversation is
# closed; ``0`` switches it off.
IDLE_CLOSE_STEPS = (0, 30, 60, 120)
DEFAULT_IDLE_CLOSE_MINUTES = 60
# A profile's own model; the empty string means the shared ``channel_model``.
PROFILE_MODELS = available_values(CLAUDE_MODEL_SPECS)
# The only keys an edit may change: ``id`` keys the session label, settings
# file and relay routes, and ``state_dir`` holds the token, so neither is ever
# writable. Fields in ``RESTART_FIELDS`` are read once, at the bot's start.
EDITABLE_FIELDS = (
    "label", "skill", "relay", "resume_offer", "idle_close_minutes", "model",
)
RESTART_FIELDS = ("skill", "relay", "model")
_LABEL_MAX = 60


@dataclass(frozen=True)
class ChannelProfile:
    id: str
    label: str
    skill: str
    state_dir: str
    relay: bool = False
    resume_offer: bool = True
    idle_close_minutes: int = DEFAULT_IDLE_CLOSE_MINUTES
    model: str = ""


def profile_session_label(profile_id: str) -> str:
    """The session-host ``label`` a profile's session carries."""
    return f"{LABEL_PREFIX}{profile_id}"


def relay_session_label(profile_id: str) -> str:
    """The session-host ``label`` of a conversation the relay opened for a profile."""
    return f"{RELAY_LABEL_PREFIX}{profile_id}"


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
                # Only a JSON true switches it on: "yes" or 1 is not consent.
                relay=row.get("relay") is True,
                **_row_options(row, where, problems),
            )
        )
    return profiles, problems


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _idle_close_ok(value: Any) -> bool:
    # ``True == 1`` in Python, so a bool must be turned away by type first.
    return isinstance(value, int) and not isinstance(value, bool) and value in IDLE_CLOSE_STEPS


def _model_ok(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in ("", *PROFILE_MODELS)


def _row_options(row: Dict[str, Any], where: str, problems: List[str]) -> Dict[str, Any]:
    """The optional fields of one row. A bad value falls back to its default
    and is reported in ``problems`` naming the field, so a hand-edit never
    takes a bot down but an edit through the API (which refuses new problems)
    can't introduce one."""
    options: Dict[str, Any] = {}
    if "resume_offer" in row:
        if isinstance(row["resume_offer"], bool):
            options["resume_offer"] = row["resume_offer"]
        else:
            problems.append(f"{where}: resume_offer must be true or false")
    if "idle_close_minutes" in row:
        if _idle_close_ok(row["idle_close_minutes"]):
            options["idle_close_minutes"] = row["idle_close_minutes"]
        else:
            problems.append(
                f"{where}: idle_close_minutes must be one of "
                + ", ".join(str(step) for step in IDLE_CLOSE_STEPS)
            )
    if "model" in row:
        if _model_ok(row["model"]):
            options["model"] = row["model"].strip().lower()
        else:
            problems.append(
                f"{where}: model must be empty or one of " + ", ".join(PROFILE_MODELS)
            )
    return options


# ------------------------------------------------------------------- edits
class ProfileEditError(ValueError):
    """A refused edit; ``status`` is the HTTP code the route answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# One lock for every read-modify-write of the file: two edits to different
# profiles each re-read it under the lock, so neither loses the other's change.
_write_lock = threading.Lock()


def _validate_changes(
    changes: Dict[str, Any], skill_ids: Optional[Collection[str]]
) -> Dict[str, Any]:
    """The cleaned changes, or the 400 naming the offending field."""
    if not changes:
        raise ProfileEditError("nothing to change")
    for key in changes:
        if key not in EDITABLE_FIELDS:
            raise ProfileEditError(f"{str(key)[:40]!r} cannot be changed here")
    clean: Dict[str, Any] = {}
    for key, value in changes.items():
        if key in ("relay", "resume_offer"):
            if not isinstance(value, bool):
                raise ProfileEditError(f"{key} must be true or false")
            clean[key] = value
        elif key == "idle_close_minutes":
            if not _idle_close_ok(value):
                raise ProfileEditError(
                    "idle_close_minutes must be one of "
                    + ", ".join(str(step) for step in IDLE_CLOSE_STEPS)
                )
            clean[key] = value
        elif key == "model":
            if not _model_ok(value):
                raise ProfileEditError(
                    "model must be empty or one of " + ", ".join(PROFILE_MODELS)
                )
            clean[key] = value.strip().lower()
        elif key == "label":
            text = _text(value)
            if not text or len(text) > _LABEL_MAX:
                raise ProfileEditError(f"label must be 1 to {_LABEL_MAX} characters")
            clean[key] = text
        else:  # skill
            text = _text(value)
            if not text or skill_ids is None or text not in skill_ids:
                raise ProfileEditError("skill is not a life-os skill")
            clean[key] = text
    return clean


def update_channel_profile(
    profile_id: str,
    changes: Dict[str, Any],
    *,
    skill_ids: Optional[Collection[str]] = None,
    path: Optional[Path] = None,
) -> Tuple[ChannelProfile, List[str]]:
    """Apply ``changes`` to one profile row; return it and the keys that differ.

    Only :data:`EDITABLE_FIELDS` are accepted. The file is re-read, edited and
    replaced under one lock: unknown keys and the other rows keep their order
    and content, the candidate is written to a temp file beside it and loaded
    with :func:`load_channel_profiles`, and an edit that adds a problem is
    refused with the file untouched. Raises :class:`ProfileEditError`.
    """
    clean = _validate_changes(changes, skill_ids)
    target = Path(path) if path is not None else DEFAULT_PROFILES_PATH
    with _write_lock:
        try:
            text = target.read_text(encoding="utf-8")
            raw = json.loads(text)
        except (OSError, ValueError) as exc:
            raise ProfileEditError(
                f"{target.name} could not be read ({type(exc).__name__})", 500
            )
        rows = raw.get("profiles") if isinstance(raw, dict) else None
        index = next(
            (i for i, row in enumerate(rows or [])
             if isinstance(row, dict) and row.get("id") == profile_id),
            None,
        )
        if index is None:
            raise ProfileEditError(f"unknown channel profile: {profile_id}", 404)
        row = rows[index]
        changed = [key for key, value in clean.items() if key not in row or row[key] != value]
        if changed:
            rows[index] = {**row, **clean}
            _replace_checked(
                target, raw, text.endswith("\n"), load_channel_profiles(target)[1]
            )
        profile = next(p for p in load_channel_profiles(target)[0] if p.id == profile_id)
    return profile, changed


def _replace_checked(
    target: Path, data: Dict[str, Any], trailing_newline: bool, before_problems: List[str]
) -> None:
    """Write ``data`` over ``target`` through a temp file in the same folder,
    after checking it loads with no problem the current file lacks."""
    temp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if trailing_newline else ""),
            encoding="utf-8",
        )
        added = [p for p in load_channel_profiles(temp)[1] if p not in before_problems]
        if added:
            raise ProfileEditError("; ".join(added))
        os.replace(temp, target)
    except OSError as exc:
        raise ProfileEditError(
            f"{target.name} could not be written ({type(exc).__name__})", 500
        )
    finally:
        temp.unlink(missing_ok=True)


# The relay's per-session environment. The CLI reads the first three;
# the token never rides a command line, so the permission rule holds no secret.
RELAY_TOKEN_ENV = "LAUNCHER_RELAY_TOKEN"
RELAY_URL_ENV = "LAUNCHER_RELAY_URL"
RELAY_PROFILE_ENV = "LAUNCHER_RELAY_PROFILE"
RELAY_COMMAND_ENV = "LAUNCHER_RELAY_COMMAND"
RELAY_SCRIPT = PROJECT_ROOT / "scripts" / "telegram_relay.py"
# The life-os skill whose bot runs two pre-approved read-only router commands.
ROUTER_SKILL = "general"


def _settings_dir() -> Path:
    directory = runtime_data_dir("app-launcher", create=True) / "channel-settings"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def relay_command() -> str:
    """The literal command a relay-enabled bot runs, before its subcommand.

    The repo's own venv interpreter when it exists (the webapp itself may run
    under ``pythonw``), then the relay script, both as forward-slash paths:
    the permission rule matches this exact text.
    """
    venv = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    interpreter = venv if venv.is_file() else Path(sys.executable)
    return f"{interpreter.as_posix()} {RELAY_SCRIPT.as_posix()}"


def _start_record_path(profile_id: str) -> Path:
    return _settings_dir() / f"{profile_id}.start.json"


def _start_record(profile: ChannelProfile, model: str) -> Dict[str, Any]:
    return {"skill": profile.skill, "relay": profile.relay, "model": model}


def restart_fields(profile: ChannelProfile, model: str) -> Optional[List[str]]:
    """The start-time fields (:data:`RESTART_FIELDS`) that differ between the
    last launch and ``profile`` / ``model`` (the model it would launch on now).

    ``[]`` means nothing changed since the start; ``None`` means the launch's
    record cannot be read, which is never folded into ``[]``.
    """
    try:
        recorded = json.loads(_start_record_path(profile.id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(recorded, dict) or any(key not in recorded for key in RESTART_FIELDS):
        return None
    current = _start_record(profile, model)
    return [key for key in RESTART_FIELDS if recorded[key] != current[key]]


def router_allow_rules(life_os_dir: Path) -> List[str]:
    """The two ``permissions.allow`` rules of the general bot's router commands.

    ``skills`` takes no argument, so its rule is the exact command. ``resume``
    takes a skill and a query, so its rule ends at ``--skill`` and nothing
    shorter or broader than that flag matches. The text is the command
    ``router.md`` tells the bot to run: the script path as a forward-slash
    absolute path, so the match is on exactly what the bot types.
    """
    script = (
        Path(life_os_dir).resolve() / ".claude" / "skills" / ROUTER_SKILL
        / "scripts" / "router_tools.py"
    ).as_posix()
    if " " in script:
        # A path with a space would need quoting the bot would have to
        # reproduce exactly for the allow rule to match.
        raise ValueError(
            "the life-os path has a space in it; move life-os to a plain path "
            "so the general bot's router commands can be pre-approved"
        )
    return [
        f"Bash(python {script} skills)",
        f"Bash(python {script} resume --skill:*)",
    ]


def write_channel_settings(
    profile: ChannelProfile,
    relay_url: str = "",
    model: str = "",
    life_os_dir: Optional[Path] = None,
) -> str:
    """Write the profile's per-session settings file; return its flag-safe path.

    Without relay the file holds only the state directory's path (a settings
    ``env`` block), never a token. With relay it also holds a new relay token,
    minted on every launch so a relaunch retires the previous one, the relay's
    URL and command, and the allow rule for that command. A profile running the
    ``general`` skill (given ``life_os_dir``) gets the allow rules of its two
    router commands (:func:`router_allow_rules`) and the context filter off,
    for the same reason relay does; no other profile does. It lives under the
    app's runtime-data directory and is rewritten on every launch, so a profile
    edit takes effect on the next start. Beside it, ``<id>.start.json`` records
    the start-time fields (:data:`RESTART_FIELDS`, with ``model`` the one the
    session launches on) for :func:`restart_fields`; it is kept out of the
    settings file so that file stays exactly what Claude reads.
    """
    target = _settings_dir() / f"{profile.id}.json"
    env = {"TELEGRAM_STATE_DIR": Path(profile.state_dir).as_posix()}
    payload: dict[str, Any] = {"env": env}
    if profile.relay:
        command = relay_command()
        if command.count(" ") != 1:
            # A path with a space would need quoting the bot would have to
            # reproduce exactly for the allow rule to match.
            raise ValueError(
                "the relay command's path has a space in it; move the "
                "app-launcher checkout to a plain path or turn relay off"
            )
        env.update({
            RELAY_TOKEN_ENV: secrets.token_urlsafe(32),
            RELAY_URL_ENV: relay_url,
            RELAY_PROFILE_ENV: profile.id,
            RELAY_COMMAND_ENV: command,
            "FLEET_CONTEXT_FILTER_MODE": "off",
        })
        payload["permissions"] = {"allow": [f"Bash({command}:*)"]}
    if profile.skill == ROUTER_SKILL and life_os_dir is not None:
        env["FLEET_CONTEXT_FILTER_MODE"] = "off"
        allow = payload.setdefault("permissions", {}).setdefault("allow", [])
        allow.extend(router_allow_rules(life_os_dir))
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _start_record_path(profile.id).write_text(
        json.dumps(_start_record(profile, model)) + "\n", encoding="utf-8"
    )
    flag_path = target.resolve().as_posix()
    if not _SETTINGS_PATH_RE.match(flag_path):
        raise ValueError(
            "the runtime-data directory path has characters that cannot ride a "
            "launch command (spaces or special characters); point "
            "APP_LAUNCHER_DATA_DIR at a plain path"
        )
    return flag_path


def relay_token_matches(profile: ChannelProfile, presented: str) -> bool:
    """Whether ``presented`` is the relay token of the profile's last launch.

    The settings file that launch wrote is the source of truth, so the check
    survives a webapp restart while the bot's session lives on. ``False`` for
    a profile without relay, an empty token, and a missing or unreadable file.
    """
    if not profile.relay or not presented:
        return False
    try:
        data = json.loads((_settings_dir() / f"{profile.id}.json").read_text(encoding="utf-8"))
        expected = data["env"][RELAY_TOKEN_ENV]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


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
