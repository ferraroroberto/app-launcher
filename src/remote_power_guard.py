"""Hide Shut down / Restart / Sleep from the Start menu only over Remote Desktop.

A mis-click in a remote session must not power off the unattended host, while
at the console every power option stays. Windows has no per-session policy for
that: ``NoClose`` under ``HKCU\\...\\Policies\\Explorer`` applies to the user,
whatever session they are in. So a scheduled task re-decides it each time the
user's session starts or changes hands: on logon (LocalSessionManager event 21)
and on reconnect (event 25) it writes ``NoClose=1`` when the session is remote
and ``0`` at the console, then restarts the Start menu if the value changed.

**Why it needs elevation.** The ``Policies`` key is read-only to the user
(SYSTEM and Administrators only), so the task runs with the highest available
privileges, and Windows lets only an elevated process register such a task. The
webapp runs unelevated: its switch tries, and on :class:`ElevationRequired`
the page shows the one command to run from an elevated terminal
(:func:`cli_command`).

**Why the script is inline.** The task's PowerShell script travels inside the
task definition as ``-EncodedCommand``, never as a file on disk: an elevated
task that runs a user-writable script file would let anything running as the
user gain admin rights by editing it.

**State is read live, three-valued.** :func:`is_enabled` asks Task Scheduler
every time, so the switch can never drift from the machine: ``True`` (task
present and enabled), ``False`` (Task Scheduler says it does not exist) or
``None`` (it could not be read), which the page shows as unknown, never off.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own folder, outside the Jobs tab's ``\AppLauncher\`` namespace, so the
# Jobs reconciler never sees or deletes it.
DEFAULT_TASK_PATH = "\\AppLauncher-Guard\\remote-power-guard"

# Env overrides for the task path and the policy key, set only by the e2e
# autoboot so the disposable webapp reads a task and a key that never exist:
# its Settings screenshots then show "Off" whatever this machine's real state.
TASK_PATH_ENV = "LAUNCHER_REMOTE_POWER_GUARD_TASK"
POLICY_KEY_ENV = "LAUNCHER_REMOTE_POWER_GUARD_KEY"

DEFAULT_POLICY_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer"
NO_CLOSE_VALUE = "NoClose"

# The LocalSessionManager events that mark a session changing hands: 21 is a
# logon, 25 a reconnect (from the console or over Remote Desktop).
SESSION_EVENT_CHANNEL = "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"
SESSION_EVENT_IDS = (21, 25)

# Task Scheduler error the "no such task / folder" case carries (HRESULT for
# ERROR_FILE_NOT_FOUND), locale-independent unlike schtasks' text.
_HRESULT_NOT_FOUND = 0x80070002
_TASK_CREATE_OR_UPDATE = 6
_TASK_LOGON_INTERACTIVE_TOKEN = 3

GuardState = Literal["on", "off", "unknown"]


class ElevationRequired(PermissionError):
    """This process is not elevated, so it cannot change the guard."""


class TaskNotFound(LookupError):
    """Task Scheduler reports the guard task (or its folder) does not exist."""


def task_path() -> str:
    """The guard task's full Task Scheduler path."""
    return os.environ.get(TASK_PATH_ENV, "").strip() or DEFAULT_TASK_PATH


def policy_key() -> str:
    """The ``HKCU`` subkey that holds ``NoClose``."""
    return os.environ.get(POLICY_KEY_ENV, "").strip() or DEFAULT_POLICY_KEY


def _split_task_path(path: str) -> tuple[str, str]:
    folder, _, name = path.rpartition("\\")
    return (folder or "\\"), name


# ------------------------------------------------------------- the task itself


# ASCII only: Windows PowerShell 5.1 misparses non-ASCII in a script it reads.
# Each run appends one line to a log under the user's profile, because a
# logon-time action that fails is otherwise invisible.
_GUARD_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$log = Join-Path $env:LOCALAPPDATA 'AppLauncher\remote-power-guard.log'
try {
  Add-Type -AssemblyName System.Windows.Forms
  $remote = [System.Windows.Forms.SystemInformation]::TerminalServerSession
  $want = 0
  if ($remote) { $want = 1 }
  $key = 'HKCU:\__POLICY_KEY__'
  $have = 0
  if (Test-Path $key) {
    $v = (Get-ItemProperty -Path $key -Name NoClose -ErrorAction SilentlyContinue).NoClose
    if ($null -ne $v) { $have = [int]$v }
  }
  $changed = $have -ne $want
  if ($changed) {
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name NoClose -PropertyType DWord -Value $want -Force | Out-Null
    $session = (Get-Process -Id $PID).SessionId
    Get-Process -Name explorer, StartMenuExperienceHost -ErrorAction SilentlyContinue |
      Where-Object { $_.SessionId -eq $session } | Stop-Process -Force
  }
  $line = "remote=$remote NoClose $have -> $want changed=$changed"
} catch {
  $line = "ERROR $($_.Exception.Message)"
}
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Add-Content -Path $log -Value ("[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $line)
"""


def guard_script(key: Optional[str] = None) -> str:
    """The PowerShell the task runs, aimed at ``key`` (default :func:`policy_key`)."""
    return _GUARD_SCRIPT.replace("__POLICY_KEY__", key or policy_key())


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _event_trigger(event_id: int) -> str:
    query = (
        f'<QueryList><Query Id="0" Path="{SESSION_EVENT_CHANNEL}">'
        f'<Select Path="{SESSION_EVENT_CHANNEL}">*[System[(EventID={event_id})]]</Select>'
        f"</Query></QueryList>"
    )
    # The short delay lets the reconnect settle, so the script reads the
    # session's new remote/console state rather than the one it is leaving.
    return (
        "<EventTrigger><Enabled>true</Enabled><Delay>PT5S</Delay>"
        f"<Subscription>{_xml_escape(query)}</Subscription></EventTrigger>"
    )


def task_xml(user_id: str, key: Optional[str] = None) -> str:
    """The task definition: both session triggers, the user's own interactive
    session, highest privileges, the inline guard script."""
    triggers = "".join(_event_trigger(event_id) for event_id in SESSION_EVENT_IDS)
    arguments = (
        "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass "
        f"-EncodedCommand {_encoded(guard_script(key))}"
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<RegistrationInfo><Description>App-launcher: hide Shut down / Restart / Sleep "
        "from the Start menu while this session is a Remote Desktop one.</Description>"
        "</RegistrationInfo>"
        f"<Triggers>{triggers}</Triggers>"
        '<Principals><Principal id="Author">'
        f"<UserId>{_xml_escape(user_id)}</UserId>"
        "<LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel>"
        "</Principal></Principals>"
        "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<ExecutionTimeLimit>PT2M</ExecutionTimeLimit><Enabled>true</Enabled>"
        "<AllowStartOnDemand>true</AllowStartOnDemand></Settings>"
        '<Actions Context="Author"><Exec>'
        "<Command>%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe</Command>"
        f"<Arguments>{arguments}</Arguments>"
        "</Exec></Actions></Task>"
    )


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    if not user:
        raise OSError("USERNAME environment variable is not set")
    return f"{domain}\\{user}" if domain else user


# ------------------------------------------------- Windows seams (faked in tests)


def is_elevated() -> bool:
    """Whether this process holds an elevated (administrator) token."""
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _connect() -> Any:
    """A connected Task Scheduler service object."""
    import win32com.client

    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def _is_not_found(exc: BaseException) -> bool:
    codes = [getattr(exc, "hresult", None)]
    excepinfo = getattr(exc, "excepinfo", None)
    if excepinfo and len(excepinfo) > 5:
        codes.append(excepinfo[5])
    return any(isinstance(c, int) and (c & 0xFFFFFFFF) == _HRESULT_NOT_FOUND for c in codes)


def _get_task(service: Any, path: str) -> Any:
    folder, name = _split_task_path(path)
    try:
        return service.GetFolder(folder).GetTask(name)
    except Exception as exc:  # pywintypes.com_error; imported lazily with win32com
        if _is_not_found(exc):
            raise TaskNotFound(path) from exc
        raise


def _ensure_folder(service: Any, folder: str) -> Any:
    try:
        return service.GetFolder(folder)
    except Exception as exc:
        if not _is_not_found(exc):
            raise
    return service.GetFolder("\\").CreateFolder(folder.lstrip("\\"))


def _read_no_close_raw(key: str) -> int:
    """``NoClose`` under ``HKCU\\<key>``; 0 when the key or value is absent."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _ = winreg.QueryValueEx(handle, NO_CLOSE_VALUE)
    except FileNotFoundError:
        return 0
    return int(value)


def _write_no_close_raw(key: str, value: int) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, NO_CLOSE_VALUE, 0, winreg.REG_DWORD, value)


def _restart_start_menu() -> None:
    """Restart Explorer and the Start menu host in this session only; Windows
    relaunches Explorer by itself, unelevated."""
    session = str(_session_id())
    for image in ("explorer.exe", "StartMenuExperienceHost.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", image, "/FI", f"SESSION eq {session}"],
            capture_output=True,
            creationflags=NO_WINDOW,
            check=False,
        )


def _session_id() -> int:
    """This process's Windows session id."""
    import ctypes
    from ctypes import wintypes

    session = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError()
    return session.value


# --------------------------------------------------------------- public surface


def is_enabled() -> Optional[bool]:
    """Whether the guard task exists and is enabled; ``None`` when unreadable."""
    if sys.platform != "win32":
        return None
    path = task_path()
    try:
        return bool(_get_task(_connect(), path).Enabled)
    except TaskNotFound:
        return False
    except Exception as exc:
        logger.warning("⚠️ remote power guard: could not read task %s: %s", path, exc)
        return None


def read_no_close() -> Optional[int]:
    """The live ``NoClose`` value (0 when absent); ``None`` when unreadable."""
    if sys.platform != "win32":
        return None
    try:
        return _read_no_close_raw(policy_key())
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ remote power guard: could not read NoClose: %s", exc)
        return None


def state() -> GuardState:
    """``on`` / ``off`` / ``unknown`` for the Settings switch."""
    enabled = is_enabled()
    if enabled is None:
        return "unknown"
    return "on" if enabled else "off"


def cli_command(action: Literal["enable", "disable"]) -> str:
    """The one line to paste into an elevated PowerShell to change the guard."""
    return (
        f"Set-Location -LiteralPath '{PROJECT_ROOT}'; "
        f".\\.venv\\Scripts\\python.exe -m src.remote_power_guard {action}"
    )


def payload() -> Dict[str, Any]:
    """The ``/api/config`` block: live state, the live ``NoClose`` value (so
    "off but power options still hidden" is visible) and both commands."""
    return {
        "state": state(),
        "no_close": read_no_close(),
        "command_enable": cli_command("enable"),
        "command_disable": cli_command("disable"),
    }


def _require_elevation() -> None:
    if not is_elevated():
        raise ElevationRequired(
            "changing the remote power guard needs an elevated (Run as administrator) terminal"
        )


def _as_os_error(action: str, exc: Exception) -> OSError:
    """A Task Scheduler (COM) failure as ``OSError``, so callers handle one
    failure type; an ``OSError`` passes through unchanged."""
    if isinstance(exc, OSError):
        return exc
    return OSError(f"Task Scheduler refused to {action} the guard task: {exc}")


def enable() -> None:
    """Register the guard task. Raises :class:`ElevationRequired` before
    touching anything when unelevated, and ``OSError`` when Task Scheduler
    refuses or the read-back does not find the task it just registered."""
    _require_elevation()
    path = task_path()
    folder_path, name = _split_task_path(path)
    try:
        folder = _ensure_folder(_connect(), folder_path)
        folder.RegisterTask(
            name, task_xml(_current_user()), _TASK_CREATE_OR_UPDATE, None, None,
            _TASK_LOGON_INTERACTIVE_TOKEN, None,
        )
    except Exception as exc:
        raise _as_os_error("register", exc) from exc
    if is_enabled() is not True:
        raise OSError(f"registered {path} but reading it back did not find it enabled")
    logger.info("✅ remote power guard: task %s registered", path)


def disable() -> bool:
    """Remove the guard task and reset ``NoClose`` to 0, so turning the guard
    off never leaves the console without power options. Returns whether the
    task existed. Raises :class:`ElevationRequired` before touching anything
    when unelevated, and ``OSError`` when Task Scheduler refuses."""
    _require_elevation()
    path = task_path()
    folder_path, name = _split_task_path(path)
    existed = True
    try:
        service = _connect()
        _get_task(service, path)
        service.GetFolder(folder_path).DeleteTask(name, 0)
    except TaskNotFound:
        existed = False
    except Exception as exc:
        raise _as_os_error("remove", exc) from exc
    key = policy_key()
    if _read_no_close_raw(key) != 0:
        _write_no_close_raw(key, 0)
        _restart_start_menu()
        logger.info("ℹ️ remote power guard: NoClose reset to 0, Start menu restarted")
    if is_enabled() is not False:
        raise OSError(f"removed {path} but reading it back still finds it")
    logger.info("✅ remote power guard: off (task %s)", "removed" if existed else "was absent")
    return existed


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m src.remote_power_guard enable|disable|status``."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.remote_power_guard")
    parser.add_argument("action", choices=("enable", "disable", "status"))
    action = parser.parse_args(argv).action
    if action == "status":
        info = payload()
        logger.info("state=%s NoClose=%s", info["state"], info["no_close"])
        return 0 if info["state"] != "unknown" else 3
    try:
        if action == "enable":
            enable()
        else:
            disable()
    except ElevationRequired as exc:
        logger.error("❌ %s", exc)
        return 2
    except OSError as exc:
        logger.error("❌ remote power guard %s failed: %s", action, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
