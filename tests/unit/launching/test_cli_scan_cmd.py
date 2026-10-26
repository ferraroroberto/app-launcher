"""app.cli.commands.scan_cmd — the `launcher scan` subcommand (issue #796).

Regression coverage for the bug that shipped with no test: `execute()`
called `discover_new(projects_dir=..., scan_root=..., existing=...)` but
`src/registry.py`'s `discover_new` only accepts `scan_root=` and
`existing=` (keyword-only), so every real invocation raised `TypeError`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.cli.commands import scan_cmd
from src.app_config import AppConfig
from src.registry import Registry


def _write_bat(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _args(dry_run: bool = False):
    return SimpleNamespace(dry_run=dry_run)


def _patch_config_and_registry(monkeypatch, scan_root: Path):
    # `projects_dir` is included even though the fixed `execute()` no
    # longer reads it, so a regression back to the old
    # `discover_new(projects_dir=...)` call still fails — on the
    # `TypeError` the bug actually raised, not on a stub missing an
    # unrelated attribute.
    monkeypatch.setattr(
        scan_cmd,
        "load_webapp_config",
        lambda: SimpleNamespace(
            apps_scan_root=str(scan_root), projects_dir=str(scan_root)
        ),
    )
    monkeypatch.setattr(
        scan_cmd, "load_registry", lambda: Registry(scan_root="", apps=[])
    )


class TestScanCommand:
    def test_dry_run_reports_new_entries_without_persisting(
        self, tmp_path: Path, monkeypatch
    ):
        scan_root = tmp_path / "scan"
        _write_bat(scan_root / "proj" / "run.bat", "streamlit run app.py")
        _patch_config_and_registry(monkeypatch, scan_root)

        persisted: list = []
        monkeypatch.setattr(
            scan_cmd,
            "persist_additions",
            lambda reg, new, root: persisted.append(new),
        )

        cmd = scan_cmd.ScanCommand(AppConfig())
        rc = cmd.execute(_args(dry_run=True))

        assert rc == 0
        assert persisted == []

    def test_persists_discovered_entries(self, tmp_path: Path, monkeypatch):
        scan_root = tmp_path / "scan"
        _write_bat(scan_root / "proj" / "run.bat", "streamlit run app.py")
        _patch_config_and_registry(monkeypatch, scan_root)

        persisted: list = []
        monkeypatch.setattr(
            scan_cmd,
            "persist_additions",
            lambda reg, new, root: persisted.append(new) or new,
        )

        cmd = scan_cmd.ScanCommand(AppConfig())
        rc = cmd.execute(_args(dry_run=False))

        assert rc == 0
        assert len(persisted) == 1
        assert len(persisted[0]) == 1

    def test_no_new_entries_is_a_clean_no_op(self, tmp_path: Path, monkeypatch):
        scan_root = tmp_path / "scan"
        scan_root.mkdir()
        _patch_config_and_registry(monkeypatch, scan_root)

        cmd = scan_cmd.ScanCommand(AppConfig())
        rc = cmd.execute(_args(dry_run=False))

        assert rc == 0
