"""Install an app on this PC from a fresh clone, or bring an installed PC's setup up to date.

Vendored verbatim (manifest key ``installer``). ``setup.bat`` runs this file as a
script, ``python -I src/installer.py``, with the repo's ``.venv`` python when it
exists, else the system python. ``-I`` keeps ``src/`` off ``sys.path``, so the
app's ``src`` package (which may import a third-party package) is never imported
and no module beside this one can shadow the standard library. Everything app-specific is declared in the ``[install]``
table of the repo's ``.fleet.toml`` (schema: project-scaffolding
``docs/installer.md``), so the bytes are the same in every app.

It is safe to run any number of times. A second run reports ``unchanged`` and
writes nothing, and nothing a PC already has configured is ever overwritten:

* ``.venv`` is created when missing, and requirements are installed when the
  requirements file changed since the last install (a hash recorded inside
  ``.venv``). An optional dependency step reruns the same way, on a change to
  the files it declares.
* Each ignored config is created from its tracked sample only when missing. A
  settings file that exists but lacks keys its sample has gets only those keys,
  after a timestamped backup, and the added key names are printed (never
  values). The top-up is refused unless git confirms the backup is ignored,
  because the backup holds the config's values. Every other config is a registry, the owner's own list, never
  rewritten. A file that does not parse is left alone and reported.
* The log-on autostart is registered only with ``-Startup`` and only when absent.

``-WhatIf`` lists every change and makes none. The run ends with a checklist of
what works and what still needs the owner, each item ``ok``, ``missing`` or
``unknown``. Exit code: 0 when every step ran, 1 when one failed, a config was
left alone because it does not parse, or a top-up was refused for its backup.

The module is stdlib only: on a fresh PC it runs on the system python before any
requirement is installed.
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A vendor-verbatim file keeps its own copy of the no-console flag: it cannot
# import a module of the adopter's tree.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The requirements and dependency-step hashes of the last successful install, inside the ignored .venv.
REQUIREMENTS_STAMP = ".install-requirements.sha256"
DEPS_STAMP = ".install-deps.sha256"
# Upper bound on one `pip install -r` or dependency step (a cold install on a slow link).
INSTALL_TIMEOUT_S = 1800
VENV_TIMEOUT_S = 300
# A checklist or autostart probe answers fast; one that hangs is reported unknown.
CHECK_TIMEOUT_S = 60
# The token in a declared command that stands for the repo's .venv python.
PYTHON_TOKEN = "{python}"

# The .gitignore line that covers every top-up backup; the backup holds the config's secrets.
BACKUP_IGNORE_PATTERN = "*.json.bak-*"

OK, MISSING, UNKNOWN = "ok", "missing", "unknown"
QUIET_STATUSES = ("unchanged", "kept", "not registered", "not checked", "left alone")

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


@dataclass
class Check:
    """One declared checklist item: files that must exist, a CLI on PATH, or a probe command."""

    name: str
    paths: list[str] = field(default_factory=list)
    which: str = ""
    command: list[str] = field(default_factory=list)
    hint: str = ""


@dataclass
class Plan:
    """The ``[install]`` table, with the defaults an app that declares nothing gets."""

    requirements: str = "requirements.txt"
    samples: list[str] = field(default_factory=lambda: ["config"])
    settings: list[str] = field(default_factory=list)
    configs: list[tuple[str, str]] = field(default_factory=list)
    deps_command: list[str] = field(default_factory=list)
    deps_inputs: list[str] = field(default_factory=list)
    startup_check: list[str] = field(default_factory=list)
    startup_register: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)


@dataclass
class Report:
    """What one run did (or, under ``-WhatIf``, would do), and what it found."""

    what_if: bool
    lines: list[tuple[str, str, str]] = field(default_factory=list)
    checklist: list[tuple[str, str, str]] = field(default_factory=list)
    failed: bool = False

    def step(self, status: str, item: str, detail: str = "") -> None:
        self.lines.append((status, item, detail))

    def fail(self, item: str, detail: str) -> None:
        self.step("failed", item, detail)
        self.failed = True

    def check(self, state: str, item: str, detail: str = "") -> None:
        self.checklist.append((state, item, detail))

    @property
    def changes(self) -> list[tuple[str, str, str]]:
        return [line for line in self.lines if line[0] not in QUIET_STATUSES]


# ---------------------------------------------------------------- the [install] table

def _str_list(table: dict[str, Any], key: str, default: list[str] | None = None) -> list[str]:
    value = table.get(key, default if default is not None else [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"[install] {key} must be a list of non-empty strings")
    return list(value)


def _subtable(table: dict[str, Any], key: str) -> dict[str, Any]:
    sub = table.get(key, {})
    if not isinstance(sub, dict):
        raise ValueError(f"[install.{key}] must be a table")
    return sub


def _checks(raw: Any) -> list[Check]:
    if not isinstance(raw, list):
        raise ValueError("[[install.check]] must be an array of tables")
    checks: list[Check] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            raise ValueError("each [[install.check]] needs a name")
        check = Check(name=entry["name"], paths=_str_list(entry, "paths"), which=str(entry.get("which", "")),
                      command=_str_list(entry, "command"), hint=str(entry.get("hint", "")))
        if sum(bool(kind) for kind in (check.paths, check.which, check.command)) != 1:
            raise ValueError(f"[[install.check]] {check.name!r} needs exactly one of paths, which, command")
        checks.append(check)
    return checks


def parse_plan(table: dict[str, Any]) -> Plan:
    """The ``[install]`` table as a ``Plan``; ValueError names the first key that is malformed."""
    plan = Plan(samples=_str_list(table, "samples", ["config"]), settings=_str_list(table, "settings"))
    requirements = table.get("requirements", plan.requirements)
    if not isinstance(requirements, str) or not requirements:
        raise ValueError("[install] requirements must be a non-empty string")
    plan.requirements = requirements
    configs = table.get("config", [])
    if not isinstance(configs, list):
        raise ValueError("[[install.config]] must be an array of tables")
    for entry in configs:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry[k] for k in ("sample", "target")):
            raise ValueError("each [[install.config]] needs a sample and a target")
        plan.configs.append((entry["sample"], entry["target"]))
    deps, startup = _subtable(table, "deps"), _subtable(table, "startup")
    plan.deps_command, plan.deps_inputs = _str_list(deps, "command"), _str_list(deps, "inputs")
    plan.startup_check, plan.startup_register = _str_list(startup, "check"), _str_list(startup, "register")
    if bool(plan.startup_check) != bool(plan.startup_register):
        raise ValueError("[install.startup] needs both check and register")
    if plan.deps_inputs and not plan.deps_command:
        raise ValueError("[install.deps] inputs need a command")
    plan.checks = _checks(table.get("check", []))
    return plan


def load_plan(root: Path) -> Plan:
    """Read ``[install]`` from ``root/.fleet.toml``; no file or no table is the default plan."""
    path = root / ".fleet.toml"
    if not path.is_file():
        return Plan()
    try:
        import tomllib
    except ModuleNotFoundError as exc:
        raise ValueError("reading .fleet.toml needs Python 3.11 or newer") from exc
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f".fleet.toml does not parse ({type(exc).__name__})") from exc
    table = data.get("install", {})
    if not isinstance(table, dict):
        raise ValueError("[install] must be a table")
    return parse_plan(table)


# ---------------------------------------------------------------- venv and dependencies

def venv_python(root: Path) -> Path:
    """The repo's own interpreter inside ``.venv``, on Windows or POSIX."""
    return root / ".venv" / "Scripts" / "python.exe" if os.name == "nt" else root / ".venv" / "bin" / "python"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stamp_matches(stamp: Path, wanted: str) -> bool:
    try:
        return stamp.read_text(encoding="ascii").strip() == wanted
    except (OSError, UnicodeDecodeError):
        return False


def _command(root: Path, command: Sequence[str]) -> list[str]:
    return [str(venv_python(root)) if part == PYTHON_TOKEN else part for part in command]


def ensure_venv(root: Path, report: Report, runner: Runner) -> None:
    """Create ``.venv`` when missing; an existing one (a junction included) is kept as it is."""
    if venv_python(root).exists():
        report.step("unchanged", ".venv")
        return
    if report.what_if:
        report.step("would create", ".venv", f"with {sys.executable}")
        return
    done = runner([sys.executable, "-m", "venv", str(root / ".venv")], timeout=VENV_TIMEOUT_S,
                  check=False, creationflags=NO_WINDOW)
    if done.returncode != 0:
        report.fail(".venv", f"python -m venv exited {done.returncode}")
        return
    report.step("created", ".venv")


def _install_when_changed(root: Path, report: Report, runner: Runner, *, item: str, stamp_name: str,
                          wanted: str, command: list[str]) -> None:
    """Run ``command`` unless the stamp inside ``.venv`` already records ``wanted``; record it on success."""
    stamp = root / ".venv" / stamp_name
    if _stamp_matches(stamp, wanted):
        report.step("unchanged", item)
        return
    if report.what_if:
        report.step("would install", item, "its inputs changed since the last install" if stamp.is_file()
                    else "no install recorded yet")
        return
    if not venv_python(root).exists():
        report.fail(item, ".venv has no python")
        return
    try:
        done = runner(command, cwd=root, timeout=INSTALL_TIMEOUT_S, check=False, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.fail(item, f"did not run ({type(exc).__name__})")
        return
    if done.returncode != 0:
        report.fail(item, f"exited {done.returncode}")
        return
    stamp.write_text(wanted + "\n", encoding="ascii")  # runtime-write-exempt: the install stamp lives in the ignored .venv
    report.step("installed", item)


def ensure_requirements(root: Path, plan: Plan, report: Report, runner: Runner) -> None:
    """Install requirements when the requirements file changed since the last recorded install."""
    reqs = root / plan.requirements
    if not reqs.is_file():
        report.fail("requirements", f"{plan.requirements} not found")
        return
    _install_when_changed(root, report, runner, item="requirements", stamp_name=REQUIREMENTS_STAMP,
                          wanted=_sha256(reqs),
                          command=[str(venv_python(root)), "-m", "pip", "install", "-r", str(reqs)])


def deps_hash(root: Path, plan: Plan) -> str | None:
    """The dependency step's fingerprint: its command and each declared input; None when an input is missing."""
    digest = hashlib.sha256(json.dumps(plan.deps_command).encode("utf-8"))
    for name in plan.deps_inputs:
        path = root / name
        if not path.is_file():
            return None
        digest.update(name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()


def ensure_deps(root: Path, plan: Plan, report: Report, runner: Runner) -> None:
    """Run the declared dependency step when its command or inputs changed since it last succeeded."""
    if not plan.deps_command:
        return
    wanted = deps_hash(root, plan)
    if wanted is None:
        report.fail("dependency step", "a declared input is missing: " + ", ".join(plan.deps_inputs))
        return
    _install_when_changed(root, report, runner, item="dependency step", stamp_name=DEPS_STAMP, wanted=wanted,
                          command=_command(root, plan.deps_command))


# ---------------------------------------------------------------- configs

def missing_keys(current: dict[str, Any], sample: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted names of the sample's keys ``current`` lacks, nested objects included; ``_comment`` keys skipped."""
    found: list[str] = []
    for key, value in sample.items():
        if key.startswith("_"):
            continue
        if key not in current:
            found.append(prefix + key)
        elif isinstance(value, dict) and isinstance(current[key], dict):
            found.extend(missing_keys(current[key], value, f"{prefix}{key}."))
    return found


def _add_missing(current: dict[str, Any], sample: dict[str, Any]) -> None:
    for key, value in sample.items():
        if key.startswith("_"):
            continue
        if key not in current:
            current[key] = value
        elif isinstance(value, dict) and isinstance(current[key], dict):
            _add_missing(current[key], value)


def _load_object(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"does not parse ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, "is not a JSON object"
    return data, ""


def config_pairs(root: Path, plan: Plan) -> list[tuple[str, str]]:
    """Every (sample, target) pair, repo-relative: each ``*.sample.json`` in the sample folders, then the declared pairs."""
    pairs: list[tuple[str, str]] = []
    for folder in plan.samples:
        for sample in sorted((root / folder).glob("*.sample.json")):
            target = sample.with_name(sample.name.replace(".sample.json", ".json"))
            pairs.append((sample.relative_to(root).as_posix(), target.relative_to(root).as_posix()))
    known = {target for _, target in pairs}
    pairs.extend(pair for pair in plan.configs if pair[1] not in known)
    return pairs


def backup_ignored(root: Path, backup: Path, runner: Runner) -> tuple[bool | None, str]:
    """Whether git ignores ``backup``: True, False, or None with the reason when git cannot tell."""
    rel = backup.relative_to(root).as_posix()
    try:
        # conventions-exempt: a vendor-verbatim file cannot import the adopter's run_git; one read-only query
        done = runner(["git", "check-ignore", "-q", "--", rel], cwd=root, timeout=CHECK_TIMEOUT_S, check=False,
                      capture_output=True, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"could not ask git whether its backup is ignored ({type(exc).__name__})"
    if done.returncode == 0:
        return True, ""
    if done.returncode == 1:
        return False, ""
    return None, f"git check-ignore exited {done.returncode} (not a git checkout?)"


def ensure_config(root: Path, sample_name: str, target_name: str, settings: bool, report: Report, stamp: str,
                  runner: Runner) -> None:
    """Create the target from its sample when missing; for a settings file, add only the keys it lacks."""
    sample, target = root / sample_name, root / target_name
    if not sample.is_file():
        report.fail(target_name, f"its sample {sample_name} is not in the checkout")
        return
    if not target.exists():
        if report.what_if:
            report.step("would create", target_name, f"from {sample.name}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)  # runtime-write-exempt: the folder the app reads its config from
        shutil.copyfile(sample, target)  # runtime-write-exempt: creates the ignored config the app reads
        report.step("created", target_name, f"from {sample.name}")
        return
    if not settings:
        report.step("unchanged", target_name, "the owner's own list, never rewritten")
        return
    sample_data, error = _load_object(sample)
    if sample_data is None:
        report.fail(target_name, f"{sample.name} {error}")
        return
    current, error = _load_object(target)
    if current is None:
        report.step("left alone", target_name, f"{error}; fix it by hand, nothing was written")
        report.failed = True
        return
    keys = missing_keys(current, sample_data)
    if not keys:
        report.step("unchanged", target_name)
        return
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    ignored, why = backup_ignored(root, backup, runner)
    if ignored is None:
        report.step("not confirmed", target_name, f"{why}; nothing was written, so no backup holding its "
                    "values can be committed")
        report.failed = True
        return
    if not ignored:
        report.step("refused", target_name, f"its backup {backup.name} would not be gitignored; add "
                    f"{BACKUP_IGNORE_PATTERN} to .gitignore and rerun, nothing was written")
        report.failed = True
        return
    if report.what_if:
        report.step("would add", target_name, "keys: " + ", ".join(keys))
        return
    shutil.copyfile(target, backup)  # runtime-write-exempt: the ignored backup beside the config it protects
    _add_missing(current, sample_data)
    # runtime-write-exempt: the ignored config the app reads, rewritten only to add missing keys
    target.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report.step("added", target_name, "keys: " + ", ".join(keys) + f" (backup {backup.name})")


def ensure_configs(root: Path, plan: Plan, report: Report, stamp: str, runner: Runner) -> None:
    for sample, target in config_pairs(root, plan):
        ensure_config(root, sample, target, target in plan.settings, report, stamp, runner)


# ---------------------------------------------------------------- probes, autostart, checklist

def probe(root: Path, command: Sequence[str], runner: Runner) -> tuple[str, str]:
    """Run a declared probe: exit 0 is ``ok``, exit 1 ``missing``; anything else, or not runnable, is ``unknown``."""
    if PYTHON_TOKEN in command and not venv_python(root).exists():
        return UNKNOWN, "not checked before .venv exists"
    try:
        done = runner(_command(root, command), cwd=root, timeout=CHECK_TIMEOUT_S, check=False,
                      capture_output=True, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return UNKNOWN, f"probe did not run ({type(exc).__name__})"
    if done.returncode == 0:
        return OK, ""
    if done.returncode == 1:
        return MISSING, ""
    return UNKNOWN, f"probe exited {done.returncode}"


def ensure_startup(root: Path, plan: Plan, report: Report, register: bool, runner: Runner) -> None:
    """Report the log-on autostart; with ``register``, add it when absent. Silent when none is declared."""
    if not plan.startup_check:
        if register:
            report.step("not registered", "start at log on", "this app declares no [install.startup]")
        return
    state, detail = probe(root, plan.startup_check, runner)
    if state == OK:
        report.step("kept", "start at log on", "already registered")
        return
    if state == UNKNOWN:
        report.step("not checked", "start at log on", detail)
        return
    if not register:
        report.step("not registered", "start at log on", "pass -Startup to add it")
        return
    if report.what_if:
        report.step("would register", "start at log on")
        return
    try:
        done = runner(_command(root, plan.startup_register), cwd=root, timeout=CHECK_TIMEOUT_S, check=False,
                      creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.fail("start at log on", f"did not run ({type(exc).__name__})")
        return
    if done.returncode != 0:
        report.fail("start at log on", f"register exited {done.returncode}")
        return
    report.step("registered", "start at log on")


def _check_state(root: Path, check: Check, runner: Runner) -> tuple[str, str]:
    if check.paths:
        return (OK, "") if all((root / name).exists() for name in check.paths) else (MISSING, "")
    if check.which:
        return (OK, "") if shutil.which(check.which) else (MISSING, f"{check.which} is not on PATH")
    return probe(root, check.command, runner)


def build_checklist(root: Path, plan: Plan, report: Report, runner: Runner) -> None:
    """What works and what still needs the owner; a fact this run could not establish is ``unknown``."""
    has_venv = venv_python(root).exists()
    report.check(OK if has_venv else MISSING, "Python environment (.venv)")
    reqs = root / plan.requirements
    stamp = root / ".venv" / REQUIREMENTS_STAMP
    if stamp.is_file() and reqs.is_file():
        state = OK if _stamp_matches(stamp, _sha256(reqs)) else MISSING
    else:
        state = UNKNOWN if has_venv else MISSING
    report.check(state, "Requirements installed")
    if plan.deps_command:
        wanted = deps_hash(root, plan)
        done = wanted is not None and _stamp_matches(root / ".venv" / DEPS_STAMP, wanted)
        report.check(OK if done else MISSING, "Dependency step")
    for _, target_name in config_pairs(root, plan):
        target = root / target_name
        if not target.exists():
            report.check(MISSING, target_name)
            continue
        if target.suffix != ".json":
            report.check(OK, target_name)
            continue
        data, error = _load_object(target)
        report.check(OK if data is not None else MISSING, target_name, error)
    for check in plan.checks:
        state, detail = _check_state(root, check, runner)
        if state != OK and check.hint:
            detail = f"{detail}; {check.hint}" if detail else check.hint
        report.check(state, check.name, detail)
    if plan.startup_check:
        state, detail = probe(root, plan.startup_check, runner)
        report.check(state, "Start at log on", detail if state != MISSING else "run: setup.bat -Startup")


# ---------------------------------------------------------------- run and render

def run(root: Path, *, what_if: bool, startup: bool = False, runner: Runner = subprocess.run,
        now: Callable[[], datetime.datetime] | None = None) -> Report:
    """One install pass over ``root``; ``runner`` stands in for ``subprocess.run`` in tests."""
    report = Report(what_if=what_if)
    try:
        plan = load_plan(root)
    except ValueError as exc:
        report.fail(".fleet.toml [install]", str(exc))
        return report
    stamp = (now or datetime.datetime.now)().strftime("%Y%m%d-%H%M%S")
    ensure_venv(root, report, runner)
    if not report.failed:
        ensure_requirements(root, plan, report, runner)
    if not report.failed:
        ensure_deps(root, plan, report, runner)
    ensure_configs(root, plan, report, stamp, runner)
    ensure_startup(root, plan, report, startup, runner)
    build_checklist(root, plan, report, runner)
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


def main(root: Path, argv: list[str] | None = None) -> int:
    """``setup.bat``'s command line: ``-WhatIf`` and ``-Startup``; returns the exit code."""
    parser = argparse.ArgumentParser(prog="setup.bat", description="Install this app on this PC, or bring its "
                                     "setup up to date. Never overwrites what this PC has configured.")
    parser.add_argument("-WhatIf", "--what-if", dest="what_if", action="store_true",
                        help="list every change and make none")
    parser.add_argument("-Startup", "--startup", dest="startup", action="store_true",
                        help="also start the app at log on (only when not already registered)")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    report = run(root, what_if=args.what_if, startup=args.startup)
    sys.stdout.write(render(report) + "\n")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(__file__).resolve().parents[1], sys.argv[1:]))
