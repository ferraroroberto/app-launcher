"""Ratchet on the fleet install standard: every missing piece is a hit, and the hits may only go down.

Every fleet app installs, reinstalls and migrates through the scaffold
installer: `setup.bat` for a fresh install, `setup.bat -MigrateFrom <folder>`
and the in-place reinstall with a dated backup, all from the one vendored
installer, with what moves declared in `.fleet.toml` and one playbook,
`docs/install.md`, with an Install, an Update and a Migrate section. This
stage reads the repo and fails when it lacks a piece the committed baseline
does not already list.

The rules (docs/install-standard-ratchet.md):

  * `installer-vendored` -- one of the four installer modules
    (`src/installer.py`, `src/installer_migrate.py`, `src/installer_declaration.py`,
    `src/installer_inplace.py`)
    is missing, is not declared under its manifest key in `[vendored]`, or its
    content is not the content this copy of the ratchet was released with,
    which is the scaffold's current one: the ratchet and the installer are
    re-vendored together, so a stale installer fails the first gate after the
    ratchet moves on. Line endings are ignored, so a CRLF checkout matches.
  * `migrate-table` -- `[install.migrate]` (with `[install.endpoint]`) is absent
    or does not parse. It is parsed by the repo's own `src/installer_migrate.py`,
    the code `setup.bat -MigrateFrom` runs, so the check and the installer can
    never disagree; with it or its `src/installer_declaration.py` missing the
    table cannot be parsed and is a hit.
  * `playbook-sections` -- the playbook is missing, or lacks an `## Install`,
    `## Update` or `## Migrate` heading (outside code fences). Other sections
    (Uninstall, Execution policy) are allowed.

Opt-in, per adopter, in its own `.fleet.toml`:

    [install_standard_ratchet]
    enabled  = true
    baseline = ".ratchets/install-standard.json"  # default
    playbook = "docs/install.md"                  # default

Exit codes: 0 pass or not enabled; 1 a hit beyond the baseline, or the baseline
is missing / a mode was refused; 2 bad configuration, baseline or arguments;
3 NOT CONFIRMED (a file that cannot be read, a `src/installer_migrate.py` that
exists but does not load) -- the gate fails on it. Modes: `check`, `--init`,
`--update-baseline` (shrink only), `--update-baseline --allow-additions`
(shows in review).

Vendor-verbatim (manifest key `install_standard_ratchet`), stdlib-only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"install standard ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys install_standard_ratchet and ratchet_baseline)")
        sys.exit(3)


_baseline = _load_sibling("ratchet_baseline")  # where the baseline lives, and its move into .ratchets/

REPO_ROOT = Path(__file__).resolve().parent.parent

RULES: tuple[str, ...] = ("installer-vendored", "migrate-table", "playbook-sections")
BASELINE_VERSION = 1
DEFAULT_BASELINE = ".ratchets/install-standard.json"
DEFAULT_PLAYBOOK = "docs/install.md"
SECTIONS: tuple[str, ...] = ("Install", "Update", "Migrate")
LABEL = "install standard ratchet"
EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# manifest key -> (path, sha256 of the content with CRLF read as LF). These are the scaffold's
# current installer modules; tests/ratchets/installer/test_install_standard_ratchet.py fails the scaffold's
# gate until they are updated in the same PR as the installer.
INSTALLER: dict[str, tuple[str, str]] = {
    "installer": ("src/installer.py", "aca52564fcf71b7e377782b3fa527f80d11d328c7dda1a832efca695b5cee906"),
    "installer_migrate": ("src/installer_migrate.py",
                          "168e9e0921fb9d0c85c29d77cf3dc7eb3a6112a2fa64632f93ad785ade29e1f5"),
    "installer_declaration": ("src/installer_declaration.py",
                              "f8730c8d8020797d95f9ae2df9b777ab09207181cd74a1f52d1065992e0593fa"),
    "installer_inplace": ("src/installer_inplace.py",
                          "0d1b606c21cd738470770a24fac838212f93ec386801b01931a10859f080678a"),
}

_CONFIG_KEYS = {"enabled", "baseline", "playbook"}
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t#]*$")


class ConfigError(RuntimeError):
    """The adopter's configuration or baseline is unusable (exit 2)."""


class NotConfirmed(RuntimeError):
    """A file could not be read or the installer module could not be loaded (exit 3)."""


# ----------------------------------------------------------------- config

def parse_config(data: dict[str, Any]) -> tuple[str, str] | None:
    """`[install_standard_ratchet]` -> (baseline, playbook), or None when the stage is not enabled."""
    table = data.get("install_standard_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[install_standard_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[install_standard_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[install_standard_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = _baseline.configured(table.get("baseline", DEFAULT_BASELINE), DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[install_standard_ratchet] baseline must be a non-empty string")
    playbook = table.get("playbook", DEFAULT_PLAYBOOK)
    if not isinstance(playbook, str) or not playbook or Path(playbook).is_absolute() or ".." in Path(playbook).parts:
        raise ConfigError("[install_standard_ratchet] playbook must be a path inside the repo")
    return baseline, playbook


def read_toml(root: Path) -> dict[str, Any]:
    path = root / ".fleet.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------ rules

def content_hash(path: Path) -> str:
    """sha256 of the file with CRLF read as LF, so an autocrlf checkout hashes like the scaffold's."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NotConfirmed(f"{path.name}: cannot be read: {exc}") from exc
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _declared(data: dict[str, Any]) -> set[str]:
    """Manifest keys the repo declares: `[vendored]`, or `[components]` in the scaffold that publishes them."""
    keys: set[str] = set()
    for table in ("vendored", "components"):
        value = data.get(table)
        if isinstance(value, dict):
            keys.update(str(k) for k in value)
    return keys


def check_installer(root: Path, data: dict[str, Any]) -> dict[str, str]:
    """`installer-vendored` hits: {hit key: why}."""
    hits: dict[str, str] = {}
    declared = _declared(data)
    for key, (rel, expected) in INSTALLER.items():
        hit = f"installer-vendored: {rel}"
        path = root / rel
        if not path.is_file():
            hits[hit] = f"missing -- vendor it from project-scaffolding (manifest key {key})"
        elif key not in declared:
            hits[hit] = f"not declared under [vendored] as `{key}`"
        elif content_hash(path) != expected:
            hits[hit] = "differs from the current scaffold copy -- re-vendor it byte for byte"
    return hits


def _load_migrate(root: Path) -> ModuleType | None:
    """The repo's own `src/installer_migrate.py`, or None when it is absent."""
    path = root / INSTALLER["installer_migrate"][0]
    if not path.is_file():
        return None
    name = "_install_standard_installer_migrate"
    # It loads its sibling installer_declaration.py by this name: never reuse another repo's copy.
    sibling = sys.modules.pop("installer_declaration", None)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses resolve their module through sys.modules
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
            sys.modules.pop("installer_declaration", None)
            if sibling is not None:
                sys.modules["installer_declaration"] = sibling
    # A missing sibling module ends the load with SystemExit, its setup.bat message.
    except (Exception, SystemExit) as exc:  # noqa: BLE001 -- any failure to load is "could not check", never a pass
        raise NotConfirmed(f"{path.name} does not load: {type(exc).__name__}: {exc}") from exc
    if not callable(getattr(module, "parse_migration", None)):
        raise NotConfirmed(f"{path.name} has no parse_migration")
    return module


def check_migrate(root: Path, data: dict[str, Any]) -> dict[str, str]:
    """`migrate-table` hits: {hit key: why}."""
    declaration = INSTALLER["installer_declaration"][0]
    if not (root / declaration).is_file():
        return {"migrate-table": f"{declaration} is missing, so [install.migrate] cannot be parsed"}
    module = _load_migrate(root)
    if module is None:
        return {"migrate-table": "src/installer_migrate.py is missing, so [install.migrate] cannot be parsed"}
    try:
        module.parse_migration(data)
    except ValueError as exc:
        return {"migrate-table": str(exc)}
    return {}


def headings(text: str) -> set[str]:
    """Level-2 heading titles outside code fences."""
    found: set[str] = set()
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING.match(line)
        if match:
            found.add(match.group(1).strip())
    return found


def check_playbook(root: Path, playbook: str) -> dict[str, str]:
    """`playbook-sections` hits: {hit key: why}."""
    path = root / playbook
    if not path.is_file():
        return {f"playbook-sections: {playbook}": "missing -- copy project-scaffolding docs/templates/install-playbook.md"}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NotConfirmed(f"{playbook}: cannot be read: {exc}") from exc
    present = headings(text)
    return {f"playbook-sections: {playbook} ## {name}": f"no `## {name}` section"
            for name in SECTIONS if name not in present}


def scan(root: Path, data: dict[str, Any], playbook: str) -> dict[str, str]:
    return {**check_installer(root, data), **check_migrate(root, data), **check_playbook(root, playbook)}


# ---------------------------------------------------------------- baseline

def read_baseline(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    hits = data.get("hits")
    if not isinstance(hits, list) or not all(isinstance(h, str) and h for h in hits):
        raise ConfigError(f"baseline {path}: `hits` must be a list of non-empty strings")
    return sorted(set(hits))


def render_baseline(hits: list[str]) -> str:
    """Sorted, one hit per line, so a baseline diff reviews cleanly."""
    lines = ",\n".join("    " + json.dumps(h, ensure_ascii=False) for h in sorted(set(hits)))
    body = "[\n" + lines + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "hits": {body}\n}}\n'


# -------------------------------------------------------------------- CLI

def _report(added: list[str], hits: dict[str, str]) -> None:
    print(f"{LABEL}: {len(added)} hit(s) beyond the baseline:")
    for hit in added:
        print(f"  [{hit.split(':')[0]}] {hit.partition(': ')[2] or hit}: {hits[hit]}")


def _load_baseline(path: Path, name: str, init: bool) -> list[str] | int:
    if init:
        if path.exists():
            print(f"{LABEL}: {name} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return []
    if not path.is_file():
        print(f"{LABEL}: FAIL: baseline {name} is missing -- run `python scripts/install_standard_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(path)
    except ConfigError as exc:
        print(f"{LABEL}: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def _write(path: Path, hits: list[str]) -> None:
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    path.write_text(render_baseline(hits), encoding="utf-8", newline="\n")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        data = read_toml(root)
        cfg = parse_config(data)
    except ConfigError as exc:
        print(f"{LABEL}: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print(f"{LABEL}: not enabled -- set [install_standard_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print(f"{LABEL}: not enabled (no [install_standard_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_name, playbook = cfg
    baseline_path = _baseline.resolve(root, baseline_name, DEFAULT_BASELINE, LABEL)
    if baseline_path is None:
        return EXIT_CONFIG
    baseline = _load_baseline(baseline_path, baseline_name, init)
    if isinstance(baseline, int):
        return baseline
    try:
        hits = scan(root, data, playbook)
    except NotConfirmed as exc:
        print(f"{LABEL}: NOT CONFIRMED: {exc}")
        print("  (a piece could not be checked, so this is neither a pass nor a hit)")
        return EXIT_NOT_CONFIRMED
    added = sorted(h for h in hits if h not in baseline)
    gone = sorted(h for h in baseline if h not in hits)

    if init:
        _write(baseline_path, list(hits))
        print(f"{LABEL}: wrote {baseline_name} with {len(hits)} hit(s)")
        return EXIT_PASS
    if update:
        if added and not allow_additions:
            _report(added, hits)
            print(f"{LABEL}: nothing written -- add the missing piece, or raise the baseline with --allow-additions")
            return EXIT_RISE
        _write(baseline_path, list(hits))
        print(f"{LABEL}: {baseline_name} now has {len(hits)} hit(s) ({len(gone)} dropped, {len(added)} added)")
        return EXIT_PASS
    if added:
        _report(added, hits)
        print("  Every app installs, reinstalls and migrates through the scaffold installer "
              "(docs/install-standard-ratchet.md). Or (reviewed) raise the baseline with "
              "`--update-baseline --allow-additions`.")
        return EXIT_RISE
    print(f"{LABEL}: PASS ({len(hits)} baselined hit(s), none new)")
    if gone:
        print(f"  {len(gone)} baselined hit(s) are fixed -- run "
              "`python scripts/install_standard_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop fixed hits (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add hits (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    # Reports contain non-ASCII; a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
