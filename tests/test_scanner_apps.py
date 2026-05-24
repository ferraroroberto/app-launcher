"""src.scanner — bat-based Apps tab discovery (issue #77)."""

from __future__ import annotations

from pathlib import Path

from src.scanner import (
    KIND_STREAMLIT,
    KIND_TUNNEL,
    KIND_WEBAPP,
    classify_bat,
    scan_app_bats,
)


def _write_bat(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestClassifyBat:
    def test_streamlit(self, tmp_path: Path):
        bat = _write_bat(tmp_path / "x.bat", "streamlit run app.py")
        assert classify_bat(bat) == KIND_STREAMLIT

    def test_webapp(self, tmp_path: Path):
        bat = _write_bat(tmp_path / "x.bat", "uvicorn app.webapp.server:app")
        assert classify_bat(bat) == KIND_WEBAPP

    def test_tunnel(self, tmp_path: Path):
        bat = _write_bat(
            tmp_path / "launch_tunnel.bat", "cloudflared tunnel run mytun"
        )
        assert classify_bat(bat) == KIND_TUNNEL

    def test_unclassified(self, tmp_path: Path):
        bat = _write_bat(tmp_path / "x.bat", "echo nothing interesting")
        assert classify_bat(bat) is None


class TestScanAppBats:
    def test_finds_top_level_bat(self, tmp_path: Path):
        _write_bat(tmp_path / "alpha" / "run.bat", "streamlit run app.py")
        found = scan_app_bats(tmp_path)
        assert [(p.name, k) for p, k in found] == [("run.bat", KIND_STREAMLIT)]

    def test_prunes_venv_and_node_modules_during_walk(self, tmp_path: Path):
        """Sentinel bats inside `.venv` and `node_modules` must not be
        returned. The point of issue #77 isn't just that they get filtered
        — it's that those directories are never descended into."""
        _write_bat(
            tmp_path / "proj" / ".venv" / "Scripts" / "activate.bat",
            "streamlit run app.py",
        )
        _write_bat(
            tmp_path / "proj" / "node_modules" / "pkg" / "bin.bat",
            "uvicorn app.webapp.server:app",
        )
        _write_bat(
            tmp_path / "proj" / "__pycache__" / "x.bat", "streamlit run app.py"
        )
        # Plus a legitimate one at the top so the test fails if the walk
        # short-circuits entirely.
        _write_bat(tmp_path / "proj" / "run.bat", "streamlit run app.py")
        found = scan_app_bats(tmp_path)
        assert [p.name for p, _ in found] == ["run.bat"]

    def test_walk_skips_pruned_dirs_without_reading_them(
        self, tmp_path: Path, monkeypatch
    ):
        """A regression guard for the actual prune semantics: planting a
        bat deep inside `.venv` must never reach `classify_bat`. If it
        does, the prune is filtering after-the-fact and the original perf
        bug is back."""
        _write_bat(
            tmp_path / "proj" / ".venv" / "Scripts" / "activate.bat",
            "streamlit run app.py",
        )
        _write_bat(tmp_path / "proj" / "run.bat", "streamlit run app.py")

        seen: list[Path] = []
        import src.scanner as scanner

        real = scanner.classify_bat

        def spy(p: Path):
            seen.append(p)
            return real(p)

        monkeypatch.setattr(scanner, "classify_bat", spy)
        scan_app_bats(tmp_path)
        assert all(".venv" not in p.parts for p in seen)

    def test_missing_root_returns_empty(self, tmp_path: Path):
        assert scan_app_bats(tmp_path / "does-not-exist") == []

    def test_results_sorted_by_path(self, tmp_path: Path):
        _write_bat(tmp_path / "b" / "run.bat", "streamlit run app.py")
        _write_bat(tmp_path / "a" / "run.bat", "streamlit run app.py")
        found = scan_app_bats(tmp_path)
        assert [p.parent.name for p, _ in found] == ["a", "b"]
