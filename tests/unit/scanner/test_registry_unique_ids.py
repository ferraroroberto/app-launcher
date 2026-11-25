"""App ids are unique per registry row: on discovery, on add and on load.

Two rows sharing an id made every by-id route act on the first match, so a
delete could remove the wrong app. A bat outside the scan root falls back to
its bare file name for an id, which is how two ``tray.bat`` files collided.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.registry import (
    AmbiguousAppId,
    AppEntry,
    Registry,
    discover_new,
    get_by_id,
    load_registry,
    persist_additions,
    remove_by_id,
    rename_by_id,
    set_autostart_by_id,
)


def _row(app_id: str, bat: str, name: str = "App") -> dict:
    return {"id": app_id, "name": name, "kind": "tray", "bat_path": bat}


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"scan_root": "", "apps": rows}), encoding="utf-8")
    return path


def _entry(app_id: str, bat: str) -> AppEntry:
    return AppEntry(id=app_id, name=app_id, kind="tray", bat_path=bat)


@pytest.fixture
def registry_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "apps.json"
    monkeypatch.setattr("src.registry.DEFAULT_REGISTRY_PATH", path)
    return path


def test_load_gives_a_later_duplicate_a_unique_id(tmp_path: Path):
    path = _write(
        tmp_path / "apps.json",
        [_row("tray", "C:/a/tray.bat"), _row("tray", "C:/b/tray.bat")],
    )
    registry = load_registry(path)
    ids = [a.id for a in registry.apps]
    assert ids[0] == "tray"
    assert len(set(ids)) == 2
    # the later row keeps its own launcher script
    assert registry.apps[1].bat_path == "C:/b/tray.bat"


def test_load_warns_once_per_duplicate(tmp_path: Path, caplog):
    path = _write(
        tmp_path / "apps.json",
        [
            _row("tray", "C:/a/tray.bat"),
            _row("tray", "C:/b/tray.bat"),
            _row("tray", "C:/c/tray.bat"),
        ],
    )
    with caplog.at_level(logging.WARNING, logger="src.registry"):
        load_registry(path)
    warnings = [r for r in caplog.records if "duplicate app id" in r.getMessage()]
    assert len(warnings) == 2
    assert all("tray" in r.getMessage() for r in warnings)


def test_migration_is_saved_so_it_runs_once(tmp_path: Path, caplog):
    path = _write(
        tmp_path / "apps.json",
        [_row("tray", "C:/a/tray.bat"), _row("tray", "C:/b/tray.bat")],
    )
    first = [a.id for a in load_registry(path).apps]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.registry"):
        second = [a.id for a in load_registry(path).apps]
    assert first == second
    assert not [r for r in caplog.records if "duplicate app id" in r.getMessage()]
    on_disk = [r["id"] for r in json.loads(path.read_text(encoding="utf-8"))["apps"]]
    assert on_disk == first


def test_a_clean_registry_is_not_rewritten(tmp_path: Path):
    path = _write(
        tmp_path / "apps.json",
        [_row("alpha", "C:/a/tray.bat"), _row("beta", "C:/b/tray.bat")],
    )
    before = path.read_bytes()
    load_registry(path)
    assert path.read_bytes() == before


def test_a_suffixed_id_never_takes_another_rows_id(tmp_path: Path):
    path = _write(
        tmp_path / "apps.json",
        [
            _row("tray", "C:/a/tray.bat"),
            _row("tray", "C:/b/tray.bat"),
            _row("tray-2", "C:/c/tray.bat"),
        ],
    )
    ids = [a.id for a in load_registry(path).apps]
    assert len(set(ids)) == 3
    assert ids[0] == "tray" and ids[2] == "tray-2"


def test_discover_new_gives_colliding_candidates_unique_ids(tmp_path: Path):
    """A new bat whose id is already a registered row's: the candidate the Apps
    tab offers carries its own id, so saving it cannot drop or double it."""
    scan_root = tmp_path / "root"
    scan_root.mkdir()
    for folder in ("one", "two"):
        (scan_root / folder).mkdir()
        (scan_root / folder / "tray.bat").write_text(
            "@echo off\ncall tray_lifecycle.ps1\n", encoding="utf-8"
        )
    existing = Registry(
        scan_root="",
        apps=[
            _entry("one-tray", "C:/elsewhere/tray.bat"),
            _entry("two-tray", "C:/other/tray.bat"),
        ],
    )
    found = discover_new(scan_root=scan_root, existing=existing, mini_root=None)
    ids = sorted(c.id for c in found)
    assert ids == ["one-tray-2", "two-tray-2"]


def test_persist_additions_never_drops_or_doubles_an_id(registry_file: Path):
    reg = Registry(scan_root="", apps=[_entry("tray", "C:/a/tray.bat")])
    added = persist_additions(
        reg,
        [_entry("tray", "C:/b/tray.bat"), _entry("tray", "C:/c/tray.bat")],
        Path("C:/stub"),
    )
    assert len(added) == 2
    ids = [a.id for a in load_registry(registry_file).apps]
    assert len(ids) == 3 and len(set(ids)) == 3


def test_persist_additions_skips_the_same_launcher_script(registry_file: Path):
    reg = Registry(scan_root="", apps=[_entry("tray", "C:/a/tray.bat")])
    added = persist_additions(reg, [_entry("tray", "C:/a/tray.bat")], Path("C:/stub"))
    assert added == []


@pytest.fixture
def ambiguous() -> Registry:
    """An in-memory registry no load produced: the helpers still refuse it."""
    return Registry(
        scan_root="",
        apps=[_entry("tray", "C:/a/tray.bat"), _entry("tray", "C:/b/tray.bat")],
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda r: get_by_id(r, "tray"),
        lambda r: remove_by_id(r, "tray"),
        lambda r: rename_by_id(r, "tray", "New"),
        lambda r: set_autostart_by_id(r, "tray", True),
    ],
    ids=["get", "remove", "rename", "autostart"],
)
def test_by_id_helpers_refuse_an_ambiguous_id(registry_file, ambiguous, call):
    with pytest.raises(AmbiguousAppId) as err:
        call(ambiguous)
    assert "tray" in str(err.value) and "2 apps" in str(err.value)
    assert len(ambiguous.apps) == 2
    assert not registry_file.exists()
