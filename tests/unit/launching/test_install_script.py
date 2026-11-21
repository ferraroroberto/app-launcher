"""``setup.bat`` / ``scripts/install.py`` keeps what a PC has configured.

Every run here targets a temp folder holding copies of the tracked samples; the
live ``config/`` and the real Startup folder are never touched.
"""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Dict

import pytest

from scripts import install

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXED_NOW = datetime.datetime(2026, 1, 2, 3, 4, 5)
BACKUP_SUFFIX = ".bak-20260102-030405"


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A checkout with only the tracked config samples in it."""
    checkout = tmp_path / "checkout"
    (checkout / "config").mkdir(parents=True)
    for sample in (REPO_ROOT / "config").glob("*.sample.json"):
        shutil.copy(sample, checkout / "config" / sample.name)
    return checkout


@pytest.fixture
def startup_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "startup"
    folder.mkdir()
    return folder


def _run(root: Path, startup_dir: Path, **kwargs: object) -> install.Report:
    return install.run(root, deps=False, startup_dir=startup_dir, now=lambda: FIXED_NOW, **kwargs)  # type: ignore[arg-type]


def test_a_fresh_checkout_gets_every_config_from_its_sample(root: Path, startup_dir: Path) -> None:
    report = _run(root, startup_dir, what_if=False)
    for sample in (root / "config").glob("*.sample.json"):
        target = sample.with_name(sample.name.replace(".sample.json", ".json"))
        assert target.read_bytes() == sample.read_bytes()
    assert {item for status, item, _ in report.lines if status == "created"} == {
        "config/apps.json", "config/channel_profiles.json", "config/config.json", "config/jobs.json",
        "config/webapp_config.json",
    }
    assert not report.failed


def test_an_existing_config_keeps_every_value_and_gains_only_missing_keys(root: Path, startup_dir: Path) -> None:
    live = {"port": 9999, "auth_password": "keep-me", "claude_model": "sonnet", "unknown_local_key": [1, 2]}
    target = root / "config" / "webapp_config.json"
    target.write_text(json.dumps(live), encoding="utf-8")
    nested = root / "config" / "config.json"
    nested.write_text(json.dumps({"tailnet_host": "", "webapp": {"port": 9001}}), encoding="utf-8")
    before = {target.name: target.read_bytes(), nested.name: nested.read_bytes()}

    report = _run(root, startup_dir, what_if=False)

    merged = json.loads(target.read_text(encoding="utf-8"))
    for key, value in live.items():
        assert merged[key] == value
    sample = json.loads((root / "config" / "webapp_config.sample.json").read_text(encoding="utf-8"))
    assert set(install.missing_keys(merged, sample)) == set()
    assert not [k for k in merged if k.startswith("_")]  # comments are not merged in
    assert json.loads(nested.read_text(encoding="utf-8"))["webapp"] == {"port": 9001, "enabled": True, "host": "0.0.0.0"}
    for name, raw in before.items():
        assert (root / "config" / f"{name}{BACKUP_SUFFIX}").read_bytes() == raw
    added = {item: detail for status, item, detail in report.lines if status == "added"}
    assert "webapp.enabled" in added["config/config.json"] and "keep-me" not in added["config/webapp_config.json"]


def test_a_registry_is_never_rewritten(root: Path, startup_dir: Path) -> None:
    jobs = root / "config" / "jobs.json"
    jobs.write_bytes(b'{"jobs":[{"id":"mine"}]}')  # no indent, no newline: must survive byte for byte
    _run(root, startup_dir, what_if=False)
    assert jobs.read_bytes() == b'{"jobs":[{"id":"mine"}]}'
    assert not list((root / "config").glob("jobs.json.bak-*"))


def test_a_config_that_does_not_parse_is_left_alone_and_reported(root: Path, startup_dir: Path) -> None:
    target = root / "config" / "webapp_config.json"
    target.write_text('{"port": 8445,', encoding="utf-8")
    report = _run(root, startup_dir, what_if=False)
    assert target.read_text(encoding="utf-8") == '{"port": 8445,'
    assert ("left alone", "config/webapp_config.json") in [(s, i) for s, i, _ in report.lines]
    assert report.failed
    assert ("missing", "config/webapp_config.json") in [(s, i) for s, i, _ in report.checklist]


def test_a_second_run_reports_unchanged_and_writes_nothing(root: Path, startup_dir: Path) -> None:
    (root / "config" / "config.json").write_text('{"log_level": "DEBUG"}', encoding="utf-8")
    _run(root, startup_dir, what_if=False)
    before = _snapshot(root)
    report = _run(root, startup_dir, what_if=False)
    assert _snapshot(root) == before
    assert report.changes == []
    assert "unchanged: nothing to do" in install.render(report)


def test_what_if_lists_every_change_and_makes_none(root: Path, startup_dir: Path) -> None:
    (root / "config" / "webapp_config.json").write_text('{"port": 9999}', encoding="utf-8")
    before = _snapshot(root)
    report = _run(root, startup_dir, what_if=True, startup=True)
    assert _snapshot(root) == before
    assert list(startup_dir.iterdir()) == []
    statuses = {item: status for status, item, _ in report.lines}
    assert statuses["config/config.json"] == "would create"
    assert statuses["config/webapp_config.json"] == "would add"
    assert statuses["start at log on"] == "would register"


def test_the_startup_entry_is_registered_only_when_asked_and_absent(root: Path, startup_dir: Path) -> None:
    report = _run(root, startup_dir, what_if=False)
    assert ("not registered", "start at log on") in [(s, i) for s, i, _ in report.lines]
    assert list(startup_dir.iterdir()) == []
    _run(root, startup_dir, what_if=False, startup=True)
    entry = next(startup_dir.iterdir())
    raw = entry.read_bytes()
    report = _run(root, startup_dir, what_if=False, startup=True)
    assert ("kept", "start at log on") in [(s, i) for s, i, _ in report.lines]
    assert entry.read_bytes() == raw


def test_the_checklist_marks_what_still_needs_the_owner(root: Path, startup_dir: Path) -> None:
    report = _run(root, startup_dir, what_if=False)
    states = {item: state for state, item, _ in report.checklist}
    assert states["Python environment (.venv)"] == "missing"
    assert states["Sign-in password"] == "missing"
    assert states["HTTPS certificate"] == "missing"
    assert states["config/webapp_config.json"] == "ok"
    assert set(states.values()) <= {"ok", "missing", "unknown"}


def test_requirements_install_only_when_requirements_changed(root: Path) -> None:
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    python = install._venv_python(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (root / ".venv" / install.REQUIREMENTS_STAMP).write_text(install._sha256(root / "requirements.txt"), encoding="ascii")
    report = install.Report(what_if=False)
    install.ensure_requirements(root, report)  # a matching stamp never reaches pip
    assert report.lines == [("unchanged", "requirements", "")]
    (root / "requirements.txt").write_text("fastapi\nhttpx\n", encoding="utf-8")
    report = install.Report(what_if=True)
    install.ensure_requirements(root, report)
    assert report.lines[0][:2] == ("would install", "requirements")


def test_every_tracked_sample_is_an_install_target() -> None:
    """A new ``config/*.sample.json`` is picked up by the glob; a settings file must have a sample."""
    samples = {p.name.replace(".sample.json", ".json") for p in (REPO_ROOT / "config").glob("*.sample.json")}
    assert set(install.SETTINGS_FILES) <= samples
