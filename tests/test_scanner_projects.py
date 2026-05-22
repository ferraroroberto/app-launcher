"""src.scanner — directory-based Claude Code discovery (issue #44)."""

from __future__ import annotations

from pathlib import Path

from src.scanner import dir_ignored, scan_project_dirs


class TestDirIgnored:
    def test_exact_name_match_case_insensitive(self):
        assert dir_ignored("Archive", ["archive"])
        assert dir_ignored("archive", ["ARCHIVE"])

    def test_glob_match(self):
        assert dir_ignored("client-old", ["*-old"])
        assert not dir_ignored("client-new", ["*-old"])

    def test_no_match_returns_false(self):
        assert not dir_ignored("keep", ["archive", "*-old"])

    def test_blank_patterns_are_ignored(self):
        assert not dir_ignored("anything", ["", "   "])


class TestScanProjectDirs:
    def test_lists_child_directories_only(self, tmp_path: Path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / "loose-file.txt").write_text("x", encoding="utf-8")
        found = scan_project_dirs(tmp_path)
        assert [p.project_dir.name for p in found] == ["alpha", "beta"]

    def test_skips_vcs_and_build_dirs(self, tmp_path: Path):
        for name in (".git", ".venv", "node_modules", "__pycache__", "real"):
            (tmp_path / name).mkdir()
        found = scan_project_dirs(tmp_path)
        assert [p.project_dir.name for p in found] == ["real"]

    def test_honours_ignore_list(self, tmp_path: Path):
        for name in ("keep", "archive", "thing-old"):
            (tmp_path / name).mkdir()
        found = scan_project_dirs(tmp_path, ["archive", "*-old"])
        assert [p.project_dir.name for p in found] == ["keep"]

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert scan_project_dirs(tmp_path / "does-not-exist") == []

    def test_ids_are_slugified(self, tmp_path: Path):
        (tmp_path / "My Project").mkdir()
        found = scan_project_dirs(tmp_path)
        assert found[0].id == "my-project"
