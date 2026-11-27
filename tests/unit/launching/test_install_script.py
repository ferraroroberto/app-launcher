"""``setup.bat`` keeps what a PC has configured, through this app's ``[install]`` table.

The run is the vendored ``src/installer.py`` over a temp git checkout holding
copies of the tracked manifest, ignore rules and config samples; the live
``config/`` and the real Startup folder are never touched. ``.venv`` is a stub,
so no probe and no pip runs; ``git check-ignore`` is real.
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import boot_autostart, installer, installer_declaration, password
from src.git_utils import run_git
from src.launching import install_checks

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXED_NOW = datetime.datetime(2026, 1, 2, 3, 4, 5)
BACKUP_SUFFIX = ".bak-20260102-030405"
SETTINGS = ["config/config.json", "config/webapp_config.json"]
# Ignored paths a migration leaves behind: the new copy makes its own.
LEFT_BEHIND = {"config/*.lock", "config/*.bak-*", "webapp/js-bundle", "webapp/briefs",
               "webapp/last_tunnel_url.txt", "webapp/restart-baseline.json", "webapp/bench", "webapp/health"}


def _stub_runner(command: List[str], **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
    if command[0] == "git":
        return subprocess.run(command, **kwargs)  # no-window-exempt: the installer passes creationflags
    return subprocess.CompletedProcess(command, 1)


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def _run(root: Path, *, what_if: bool = False) -> installer.Report:
    return installer.run(root, what_if=what_if, runner=_stub_runner, now=lambda: FIXED_NOW)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A git checkout with the tracked manifest and samples, its requirements already stamped."""
    checkout = tmp_path / "checkout"
    (checkout / "config").mkdir(parents=True)
    for sample in (REPO_ROOT / "config").glob("*.sample.json"):
        shutil.copy(sample, checkout / "config" / sample.name)
    for name in (".fleet.toml", ".gitignore", "requirements.txt"):
        shutil.copy(REPO_ROOT / name, checkout / name)
    # A settings top-up is refused unless git confirms its backup is ignored.
    assert run_git(checkout, ["init", "-q"]) is not None
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


def _migration() -> installer_declaration.Migration:
    return installer_declaration.parse_migration(installer_declaration.read_fleet_toml(REPO_ROOT))


def test_every_ignored_config_and_state_path_is_carried_over_or_left_behind_by_name() -> None:
    """A state file missing from [install.migrate] is lost by a migration."""
    ignored = [line.strip().rstrip("/") for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    # The gate's own scratch files are never part of an install.
    paths = [e for e in ignored if e.startswith(("config/", "webapp/")) and not e.startswith("webapp/e2e-")]
    mig = _migration()
    assert [p for p in paths if p not in mig.copy and p not in LEFT_BEHIND] == []
    assert set(mig.secrets) <= set(mig.copy) | {"webapp/certificates/key.pem"}
    playbook = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    assert [p for p in paths if p.split("/", 1)[1] not in playbook] == []


def test_migration_verify_asks_the_port_this_pc_serves(tmp_path: Path) -> None:
    endpoint = _migration().endpoint
    assert endpoint is not None
    assert installer_declaration.local_port(tmp_path, endpoint)[0] == 8445
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text('{"webapp": {"port": 9001}}', encoding="utf-8")
    assert installer_declaration.local_port(tmp_path, endpoint)[0] == 9001


def test_the_password_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    config = tmp_path / "config" / "webapp_config.json"
    assert install_checks.password_state(tmp_path) == install_checks.MISSING
    config.write_text('{"auth_password": "legacy"}', encoding="utf-8")
    assert install_checks.password_state(tmp_path) == install_checks.OK
    # A Windows editor may save the config with a BOM.
    config.write_text('{"auth_password": "legacy"}', encoding="utf-8-sig")
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
