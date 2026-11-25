"""``setup.bat`` keeps what a PC has configured, through this app's ``[install]`` table.

The run is the vendored ``src/installer.py`` over a temp checkout holding copies
of the tracked manifest and config samples; the live ``config/`` and the real
Startup folder are never touched. ``.venv`` is a stub, so no probe and no pip
runs.
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import boot_autostart, installer, password
from src.launching import install_checks

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXED_NOW = datetime.datetime(2026, 1, 2, 3, 4, 5)
BACKUP_SUFFIX = ".bak-20260102-030405"
SETTINGS = ["config/config.json", "config/webapp_config.json"]


def _stub_runner(command: List[str], **_: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.CompletedProcess(command, 1)


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _run(root: Path, *, what_if: bool = False) -> installer.Report:
    return installer.run(root, what_if=what_if, runner=_stub_runner, now=lambda: FIXED_NOW)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A checkout with the tracked manifest and samples, its requirements already stamped."""
    checkout = tmp_path / "checkout"
    (checkout / "config").mkdir(parents=True)
    for sample in (REPO_ROOT / "config").glob("*.sample.json"):
        shutil.copy(sample, checkout / "config" / sample.name)
    for name in (".fleet.toml", "requirements.txt"):
        shutil.copy(REPO_ROOT / name, checkout / name)
    python = installer.venv_python(checkout)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    stamp = checkout / ".venv" / installer.REQUIREMENTS_STAMP
    stamp.write_text(installer._sha256(checkout / "requirements.txt"), encoding="ascii")
    return checkout


def test_the_manifest_declares_this_apps_settings_and_checks() -> None:
    plan = installer.load_plan(REPO_ROOT)
    assert plan.settings == SETTINGS
    targets = {target for _, target in installer.config_pairs(REPO_ROOT, plan)}
    assert set(SETTINGS) < targets
    assert {"config/apps.json", "config/jobs.json", "config/channel_profiles.json"} <= targets - set(plan.settings)
    # Every declared probe names one this app implements.
    probes = [c.command for c in plan.checks if c.command] + [plan.startup_check, plan.startup_register]
    assert {command[-1] for command in probes} == set(install_checks.PROBES)


def test_existing_configs_keep_values_and_gain_missing_keys_after_a_backup(root: Path) -> None:
    live = {"port": 9999, "auth_password": "keep-me", "claude_model": "sonnet", "unknown_local_key": [1, 2]}
    target = root / "config" / "webapp_config.json"
    target.write_text(json.dumps(live), encoding="utf-8")
    jobs = root / "config" / "jobs.json"
    jobs.write_bytes(b'{"jobs":[{"id":"mine"}]}')  # a registry: survives byte for byte
    before = target.read_bytes()

    report = _run(root)

    merged = json.loads(target.read_text(encoding="utf-8"))
    assert {key: merged[key] for key in live} == live
    sample = json.loads((root / "config" / "webapp_config.sample.json").read_text(encoding="utf-8"))
    assert installer.missing_keys(merged, sample) == []
    assert (root / "config" / f"webapp_config.json{BACKUP_SUFFIX}").read_bytes() == before
    assert jobs.read_bytes() == b'{"jobs":[{"id":"mine"}]}'
    assert not any("keep-me" in detail for _, _, detail in report.lines)
    assert not report.failed


def test_a_second_run_is_unchanged_and_what_if_writes_nothing(root: Path) -> None:
    before = _snapshot(root)
    planned = _run(root, what_if=True)
    assert _snapshot(root) == before
    assert {"config/config.json", "config/webapp_config.json"} <= {item for _, item, _ in planned.changes}
    _run(root)
    after = _snapshot(root)
    report = _run(root)
    assert _snapshot(root) == after
    assert report.changes == []


def test_the_migrate_playbook_names_every_ignored_config_and_state_path() -> None:
    """A state file missing from docs/install.md is left behind by a migration."""
    ignored = [line.strip().rstrip("/") for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    playbook = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    names = [entry.split("/", 1)[1] for entry in ignored if entry.startswith(("config/", "webapp/"))]
    # The gate's own scratch files are never part of an install.
    assert [n for n in names if not n.startswith("e2e-") and n not in playbook] == []


def test_the_password_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    config = tmp_path / "config" / "webapp_config.json"
    assert install_checks.password_state(tmp_path) == install_checks.MISSING
    config.write_text('{"auth_password": "legacy"}', encoding="utf-8")
    assert install_checks.password_state(tmp_path) == install_checks.OK
    config.write_text("{}", encoding="utf-8")
    password.set_password("1234", tmp_path / "config" / "app_password.json")
    assert install_checks.password_state(tmp_path) == install_checks.OK
    # An unreadable config is "unknown", never the "missing" an uncaught error would exit with.
    config.write_text("{", encoding="utf-8")
    monkeypatch.setitem(install_checks.PROBES, "password", lambda: install_checks.password_state(tmp_path))
    assert install_checks.main(["password"]) == install_checks.UNKNOWN


def test_autostart_is_ok_only_for_an_entry_that_starts_this_checkout(tmp_path: Path) -> None:
    mine, older = tmp_path / "new" / "tray.bat", tmp_path / "old" / "tray.bat"
    assert install_checks.autostart_state(tmp_path, mine) == install_checks.MISSING
    boot_autostart.enable(tray_bat=older, startup_dir=tmp_path)
    assert install_checks.autostart_state(tmp_path, mine) == install_checks.UNKNOWN
    boot_autostart.enable(tray_bat=mine, startup_dir=tmp_path)
    assert install_checks.autostart_state(tmp_path, mine) == install_checks.OK
