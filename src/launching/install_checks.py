"""app-launcher's own probes for ``setup.bat``'s checklist and ``-Startup``.

``.fleet.toml``'s ``[install]`` table runs each as
``{python} -m src.launching.install_checks <name>``. The installer reads the exit
code: 0 is ``ok``, 1 is ``missing``, anything else is ``unknown``. A probe that
cannot establish its fact exits 2, so it is never read as either answer.

``autostart`` is ``ok`` only when the log-on entry starts this checkout's
``tray.bat``. An entry that starts another folder (an older copy of the app)
exits 2, so ``-Startup`` leaves it alone and the checklist does not call it done.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src import boot_autostart, password

OK, MISSING, UNKNOWN = 0, 1, 2

ROOT = Path(__file__).resolve().parents[2]
TRAY_HELPER = Path.home() / ".claude" / "tray" / "tray_lifecycle.ps1"


def password_state(root: Path = ROOT) -> int:
    """A stored hash, or the legacy ``auth_password`` the login route still accepts."""
    legacy = ""
    config = root / "config" / "webapp_config.json"
    if config.is_file():
        data = json.loads(config.read_text(encoding="utf-8"))
        legacy = str(data.get("auth_password") or "")
    stored = password.is_configured(legacy=legacy, path=root / "config" / "app_password.json")
    return OK if stored else MISSING


def autostart_state(startup_dir: Optional[Path] = None, tray_bat: Path = boot_autostart.TRAY_BAT_PATH) -> int:
    """Whether the log-on entry exists and starts ``tray_bat``."""
    entry = boot_autostart.wrapper_bat_path(startup_dir)
    if not entry.is_file():
        return MISSING
    text = entry.read_text(encoding="utf-8").lower()
    return OK if f'call "{tray_bat}"'.lower() in text else UNKNOWN


def autostart_enable(startup_dir: Optional[Path] = None) -> int:
    boot_autostart.enable(startup_dir=startup_dir)
    return OK


def tray_helper_state() -> int:
    return OK if TRAY_HELPER.is_file() else MISSING


PROBES: Dict[str, Callable[[], int]] = {
    "password": password_state,
    "autostart": autostart_state,
    "autostart-enable": autostart_enable,
    "tray-helper": tray_helper_state,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.launching.install_checks", description=__doc__)
    parser.add_argument("probe", choices=sorted(PROBES))
    args = parser.parse_args(argv)
    try:
        return PROBES[args.probe]()
    # An uncaught error would exit 1, which the installer reads as "missing".
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{args.probe} not established: {type(exc).__name__}\n")
        return UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main())
