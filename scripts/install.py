"""Install app-launcher on this PC, or bring an installed PC's setup up to date.

``setup.bat`` runs this with the repo's ``.venv`` python when it exists, else the
system python. It is safe to run any number of times. A second run reports
``unchanged`` and writes nothing, and nothing a PC already has configured is
ever overwritten:

* ``.venv`` is created when missing, and requirements are installed when
  ``requirements.txt`` changed since the last install (a hash recorded inside
  ``.venv``).
* Each ignored ``config/*.json`` is created from its tracked ``*.sample.json``
  only when missing. A settings file (``SETTINGS_FILES``) that exists but lacks
  keys its sample has gets only those keys, after a timestamped backup, and the
  added key names are printed (never values). A registry (apps, jobs, channel
  profiles) is the owner's own list and is never rewritten. A file that does
  not parse is left alone and reported.
* The log-on autostart entry is registered only with ``-Startup`` and only when
  absent; an existing one is reported ``kept``.

``-WhatIf`` lists every change and makes none. The run ends with a checklist of
what works and what still needs the owner, each item ``ok``, ``missing`` or
``unknown``. Exit code: 0 when every step ran, 1 when one failed or a config
was left alone because it does not parse.

Usage (from the repo root)::

    setup.bat                 # install or update this PC
    setup.bat -WhatIf         # list the changes, make none
    setup.bat -Startup        # also start the tray at log on
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `src` modules load lazily: the package imports third-party modules, and under
# -WhatIf on a fresh PC this runs on the system python before requirements exist.
# The no-console flag is read here for the same reason.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Settings files: an existing copy gains the keys its sample has and it lacks.
# Every other sample is a registry, created when missing and never rewritten.
SETTINGS_FILES = ("config.json", "webapp_config.json")
# The requirements hash of the last successful install, inside the ignored .venv.
REQUIREMENTS_STAMP = ".install-requirements.sha256"
# Upper bound on one `pip install -r` (a cold install on a slow link).
PIP_TIMEOUT_S = 1800
VENV_TIMEOUT_S = 300
TRAY_HELPER = Path.home() / ".claude" / "tray" / "tray_lifecycle.ps1"

OK, MISSING, UNKNOWN = "ok", "missing", "unknown"


@dataclass
class Report:
    """What one run did (or, under ``-WhatIf``, would do), and what it found."""

    what_if: bool
    lines: List[Tuple[str, str, str]] = field(default_factory=list)
    checklist: List[Tuple[str, str, str]] = field(default_factory=list)
    failed: bool = False

    def step(self, status: str, item: str, detail: str = "") -> None:
        self.lines.append((status, item, detail))

    def check(self, state: str, item: str, detail: str = "") -> None:
        self.checklist.append((state, item, detail))

    @property
    def changes(self) -> List[Tuple[str, str, str]]:
        return [line for line in self.lines if line[0] not in ("unchanged", "kept", "not registered", "not checked", "left alone")]


def _venv_python(root: Path) -> Path:
    return root / ".venv" / "Scripts" / "python.exe" if os.name == "nt" else root / ".venv" / "bin" / "python"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_venv(root: Path, report: Report) -> None:
    """Create ``.venv`` when missing; an existing one (a junction included) is kept as it is."""
    if _venv_python(root).exists():
        report.step("unchanged", ".venv")
        return
    if report.what_if:
        report.step("would create", ".venv", f"with {sys.executable}")
        return
    done = subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], timeout=VENV_TIMEOUT_S,
                          check=False, creationflags=NO_WINDOW)
    if done.returncode != 0:
        report.step("failed", ".venv", f"python -m venv exited {done.returncode}")
        report.failed = True
        return
    report.step("created", ".venv")


def ensure_requirements(root: Path, report: Report) -> None:
    """Install requirements when ``requirements.txt`` changed since the last recorded install."""
    reqs = root / "requirements.txt"
    stamp = root / ".venv" / REQUIREMENTS_STAMP
    if not reqs.is_file():
        report.step("failed", "requirements", "requirements.txt not found")
        report.failed = True
        return
    wanted = _sha256(reqs)
    if stamp.is_file() and stamp.read_text(encoding="ascii").strip() == wanted:
        report.step("unchanged", "requirements")
        return
    if report.what_if:
        why = "requirements.txt changed since the last install" if stamp.is_file() else "no install recorded yet"
        report.step("would install", "requirements", why)
        return
    python = _venv_python(root)
    if not python.exists():
        report.step("failed", "requirements", ".venv has no python")
        report.failed = True
        return
    done = subprocess.run([str(python), "-m", "pip", "install", "-r", str(reqs)], cwd=root,
                          timeout=PIP_TIMEOUT_S, check=False, creationflags=NO_WINDOW)
    if done.returncode != 0:
        report.step("failed", "requirements", f"pip install exited {done.returncode}")
        report.failed = True
        return
    # runtime-write-exempt: the install stamp lives in the ignored .venv
    stamp.write_text(wanted + "\n", encoding="ascii")
    report.step("installed", "requirements")


def missing_keys(current: Dict[str, Any], sample: Dict[str, Any], prefix: str = "") -> List[str]:
    """Dotted names of the sample's keys ``current`` lacks, nested objects included; ``_comment`` keys skipped."""
    found: List[str] = []
    for key, value in sample.items():
        if key.startswith("_"):
            continue
        if key not in current:
            found.append(prefix + key)
        elif isinstance(value, dict) and isinstance(current[key], dict):
            found.extend(missing_keys(current[key], value, f"{prefix}{key}."))
    return found


def _add_missing(current: Dict[str, Any], sample: Dict[str, Any]) -> None:
    for key, value in sample.items():
        if key.startswith("_"):
            continue
        if key not in current:
            current[key] = value
        elif isinstance(value, dict) and isinstance(current[key], dict):
            _add_missing(current[key], value)


def _load_object(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"does not parse ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, "is not a JSON object"
    return data, ""


def ensure_config(sample: Path, target: Path, report: Report, stamp: str) -> None:
    """Create ``target`` from ``sample`` when missing; for a settings file, add only the keys it lacks."""
    name = f"config/{target.name}"
    if not target.exists():
        if report.what_if:
            report.step("would create", name, f"from {sample.name}")
            return
        shutil.copyfile(sample, target)  # runtime-write-exempt: creates the ignored config/<name>.json the app reads
        report.step("created", name, f"from {sample.name}")
        return
    if target.name not in SETTINGS_FILES:
        report.step("unchanged", name, "the owner's own list, never rewritten")
        return
    sample_data, error = _load_object(sample)
    if sample_data is None:
        report.step("failed", name, f"{sample.name} {error}")
        report.failed = True
        return
    current, error = _load_object(target)
    if current is None:
        report.step("left alone", name, f"{error}; fix it by hand, nothing was written")
        report.failed = True
        return
    keys = missing_keys(current, sample_data)
    if not keys:
        report.step("unchanged", name)
        return
    if report.what_if:
        report.step("would add", name, "keys: " + ", ".join(keys))
        return
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copyfile(target, backup)  # runtime-write-exempt: the ignored backup beside the config it protects
    _add_missing(current, sample_data)
    # runtime-write-exempt: the ignored config/<name>.json the app reads, rewritten only to add missing keys
    target.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report.step("added", name, "keys: " + ", ".join(keys) + f" (backup {backup.name})")


def ensure_configs(root: Path, report: Report, stamp: str) -> None:
    for sample in sorted((root / "config").glob("*.sample.json")):
        ensure_config(sample, sample.with_name(sample.name.replace(".sample.json", ".json")), report, stamp)


def ensure_startup(report: Report, register: bool, startup_dir: Optional[Path], tray_bat: Path) -> None:
    """Report the log-on autostart entry; with ``register``, add it when absent."""
    from src import boot_autostart

    if boot_autostart.is_enabled(startup_dir):
        report.step("kept", "start at log on", "already registered")
        return
    if not register:
        report.step("not registered", "start at log on", "pass -Startup, or use Settings, to add it")
        return
    if report.what_if:
        report.step("would register", "start at log on")
        return
    boot_autostart.enable(tray_bat=tray_bat, startup_dir=startup_dir)
    report.step("registered", "start at log on")


def _signin_state(root: Path) -> Tuple[str, str]:
    current, _ = _load_object(root / "config" / "webapp_config.json")
    if current is None:
        return UNKNOWN, "webapp_config.json not readable"
    try:
        from src import password
    except ImportError as exc:
        return UNKNOWN, f"not checked before requirements are installed ({exc.name})"
    legacy = str(current.get("auth_password") or "")
    if password.is_configured(legacy=legacy, path=root / "config" / "app_password.json"):
        return OK, ""
    return MISSING, "set one with: .venv\\Scripts\\python.exe -m src.password set"


def build_checklist(root: Path, report: Report, startup_dir: Optional[Path]) -> None:
    """What works and what still needs the owner; a fact this run could not establish is ``unknown``."""
    report.check(OK if _venv_python(root).exists() else MISSING, "Python environment (.venv)")
    stamp = root / ".venv" / REQUIREMENTS_STAMP
    reqs = root / "requirements.txt"
    if stamp.is_file() and reqs.is_file():
        state = OK if stamp.read_text(encoding="ascii").strip() == _sha256(reqs) else MISSING
    else:
        state = UNKNOWN if _venv_python(root).exists() else MISSING
    report.check(state, "Requirements installed")
    for sample in sorted((root / "config").glob("*.sample.json")):
        target = sample.with_name(sample.name.replace(".sample.json", ".json"))
        if not target.exists():
            report.check(MISSING, f"config/{target.name}")
            continue
        data, error = _load_object(target)
        report.check(OK if data is not None else MISSING, f"config/{target.name}", error)
    signin, signin_detail = _signin_state(root)
    report.check(signin, "Sign-in password", signin_detail)
    certs = root / "webapp" / "certificates"
    has_cert = (certs / "cert.pem").is_file() and (certs / "key.pem").is_file()
    report.check(OK if has_cert else MISSING, "HTTPS certificate",
                 "" if has_cert else "run: .venv\\Scripts\\python.exe scripts\\gen_tailscale_cert.py")
    report.check(OK if TRAY_HELPER.is_file() else MISSING, "Shared tray helper (tray.bat --restart)",
                 "" if TRAY_HELPER.is_file() else "a plain tray.bat start works without it")
    try:
        from src import boot_autostart

        report.check(OK if boot_autostart.is_enabled(startup_dir) else MISSING, "Start at log on")
    except ImportError as exc:  # not established is its own state, never a pass
        report.check(UNKNOWN, "Start at log on", f"not checked before requirements are installed ({exc.name})")


def run(root: Path, *, what_if: bool, startup: bool = False, deps: bool = True,
        startup_dir: Optional[Path] = None, now: Optional[Callable[[], datetime.datetime]] = None) -> Report:
    """One install pass over ``root``. ``deps=False`` skips the venv and pip steps (tests)."""
    report = Report(what_if=what_if)
    stamp = (now or datetime.datetime.now)().strftime("%Y%m%d-%H%M%S")
    if deps:
        ensure_venv(root, report)
        if not report.failed:
            ensure_requirements(root, report)
    ensure_configs(root, report, stamp)
    try:
        ensure_startup(report, startup, startup_dir, root / "tray.bat")
    except ImportError as exc:
        report.step("not checked", "start at log on", f"requirements are not installed yet ({exc.name})")
    except OSError as exc:  # a failed registration is reported; the rest of the install still counts
        report.step("failed", "start at log on", type(exc).__name__)
        report.failed = True
    build_checklist(root, report, startup_dir)
    return report


def render(report: Report) -> str:
    out = ["What-if: nothing below is changed." if report.what_if else "Install steps:"]
    out += [f"  {status:<15} {item}" + (f"  ({detail})" if detail else "") for status, item, detail in report.lines]
    if not report.changes:
        out.append("  -> unchanged: nothing to do")
    out.append("")
    out.append("Checklist:")
    out += [f"  [{state:<7}] {item}" + (f"  - {detail}" if detail else "") for state, item, detail in report.checklist]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="setup.bat", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-WhatIf", "--what-if", dest="what_if", action="store_true",
                        help="list every change and make none")
    parser.add_argument("-Startup", "--startup", dest="startup", action="store_true",
                        help="also start the tray at log on (only when not already registered)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    report = run(REPO_ROOT, what_if=args.what_if, startup=args.startup)
    sys.stdout.write(render(report) + "\n")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
