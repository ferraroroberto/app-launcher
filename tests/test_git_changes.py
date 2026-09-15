"""src/git_changes.py — the working-tree list + per-file diff behind the
Coding row's ``⋯`` menu (#977).

Every case runs against a real throwaway ``git`` repository built in
``tmp_path``: the module's whole job is parsing what git actually prints
(porcelain v2 with ``-z``, ``--numstat -z`` rename records), so a mocked
``run_git`` would only pin the author's memory of that format.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from src import git_changes
from src.git_changes import (
    EMPTY_TREE,
    file_diff,
    list_changes,
    open_folder,
    safe_relative_path,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One commit with three tracked files, identity + autocrlf pinned."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    # The user-level hooksPath rejects any commit author off its allowlist.
    _git(r, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (r / "keep.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (r / "gone.txt").write_text("bye\n", encoding="utf-8")
    (r / "old-name.txt").write_text("same content\n" * 5, encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


class TestListChanges:
    def test_clean_tree_lists_nothing(self, repo: Path):
        changes = list_changes(repo)
        assert changes.branch == "main"
        assert changes.files == []
        assert changes.to_dict()["counts"] == {"files": 0, "additions": 0, "deletions": 0}

    def test_every_status_kind_with_counts_and_staged_flag(self, repo: Path):
        (repo / "keep.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")   # M, unstaged
        (repo / "gone.txt").unlink()                                                    # D
        (repo / "old-name.txt").rename(repo / "new-name.txt")                           # R (staged)
        _git(repo, "add", "-A", "old-name.txt", "new-name.txt")
        (repo / "fresh.txt").write_text("a\nb\n", encoding="utf-8")                     # A (staged)
        _git(repo, "add", "fresh.txt")
        sub = repo / "notes"
        sub.mkdir()
        (sub / "todo.md").write_text("x\ny\nz", encoding="utf-8")                       # U (no final newline)
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary")                          # U, binary

        by_path = {f.path: f for f in list_changes(repo).files}

        assert by_path["keep.txt"].status == "M"
        assert by_path["keep.txt"].staged is False
        assert (by_path["keep.txt"].additions, by_path["keep.txt"].deletions) == (2, 1)

        assert by_path["gone.txt"].status == "D"
        assert (by_path["gone.txt"].additions, by_path["gone.txt"].deletions) == (0, 1)

        assert by_path["new-name.txt"].status == "R"
        assert by_path["new-name.txt"].old_path == "old-name.txt"
        assert by_path["new-name.txt"].staged is True
        assert (by_path["new-name.txt"].additions, by_path["new-name.txt"].deletions) == (0, 0)

        assert by_path["fresh.txt"].status == "A"
        assert by_path["fresh.txt"].staged is True
        assert (by_path["fresh.txt"].additions, by_path["fresh.txt"].deletions) == (2, 0)

        assert by_path["notes/todo.md"].status == "U"
        assert by_path["notes/todo.md"].staged is False
        assert (by_path["notes/todo.md"].additions, by_path["notes/todo.md"].deletions) == (3, 0)

        assert by_path["blob.bin"].status == "U"
        assert by_path["blob.bin"].binary is True
        assert by_path["blob.bin"].additions is None

        # Untracked entries sort after tracked changes; each group is by path.
        order = [f.path for f in list_changes(repo).files]
        assert order == ["fresh.txt", "gone.txt", "keep.txt", "new-name.txt", "blob.bin", "notes/todo.md"]

        counts = list_changes(repo).to_dict()["counts"]
        assert counts == {"files": 6, "additions": 7, "deletions": 2}

    def test_fresh_repo_without_a_commit_uses_the_empty_tree(self, tmp_path: Path):
        r = tmp_path / "fresh"
        r.mkdir()
        _git(r, "init", "-q", "-b", "main")
        _git(r, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
        (r / "a.txt").write_text("hello\n", encoding="utf-8")
        _git(r, "add", "a.txt")
        (r / "b.txt").write_text("loose\n", encoding="utf-8")

        assert git_changes._base_ref(r) == EMPTY_TREE
        by_path = {f.path: f for f in list_changes(r).files}
        assert by_path["a.txt"].status == "A" and by_path["a.txt"].staged is True
        assert (by_path["a.txt"].additions, by_path["a.txt"].deletions) == (1, 0)
        assert by_path["b.txt"].status == "U"


class TestFileDiff:
    def test_modified_tracked_file_is_a_real_git_diff(self, repo: Path):
        (repo / "keep.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
        d = file_diff(repo, "keep.txt")
        assert d.path == "keep.txt"
        assert d.binary is False and d.truncated is False
        assert "-two\n" in d.diff and "+TWO\n" in d.diff
        assert d.diff.startswith("diff --git")
        assert d.diff.endswith("\n")

    def test_deleted_file_is_all_minus_lines(self, repo: Path):
        (repo / "gone.txt").unlink()
        d = file_diff(repo, "gone.txt")
        assert "-bye\n" in d.diff and "deleted file mode" in d.diff

    def test_untracked_text_file_is_synthesized_not_no_index(self, repo: Path):
        (repo / "notes.md").write_text("x\ny\nz", encoding="utf-8", newline="\n")
        d = file_diff(repo, "notes.md")
        assert d.diff == (
            "--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1,3 @@\n"
            "+x\n+y\n+z\n\\ No newline at end of file\n"
        )
        assert d.binary is False and d.truncated is False

    def test_untracked_binary_is_flagged_with_empty_diff(self, repo: Path):
        (repo / "blob.bin").write_bytes(b"\x89PNG\x00\x00junk")
        d = file_diff(repo, "blob.bin")
        assert d.binary is True and d.diff == ""

    def test_unchanged_tracked_file_has_empty_diff(self, repo: Path):
        d = file_diff(repo, "keep.txt")
        assert d.diff == "" and d.binary is False

    def test_truncation_cuts_on_a_line_boundary(self, repo: Path):
        (repo / "big.txt").write_text("".join(f"line {i:05d}\n" for i in range(5000)), encoding="utf-8")
        d = file_diff(repo, "big.txt", max_bytes=2000)
        assert d.truncated is True
        assert len(d.diff.encode("utf-8")) <= 2000
        assert d.diff.endswith("\n")
        # Every line is intact — no half line at the cut.
        assert all(line.startswith(("+", "-", "@@")) for line in d.diff.splitlines())

    def test_backslash_path_is_normalised(self, repo: Path):
        sub = repo / "notes"
        sub.mkdir()
        (sub / "a.md").write_text("q\n", encoding="utf-8", newline="\n")
        d = file_diff(repo, "notes\\a.md")
        assert d.path == "notes/a.md"
        assert "+q\n" in d.diff


class TestPathSafety:
    @pytest.mark.parametrize("bad", [
        "", "   ", "..", "../x", "a/../../x", "./a", "a/./b", "/etc/passwd", "\\x",
        "C:\\Windows\\win.ini", "C:x", "a//b", "a/",
    ])
    def test_rejected_paths(self, repo: Path, bad: str):
        with pytest.raises(ValueError):
            safe_relative_path(repo, bad)
        with pytest.raises(ValueError):
            file_diff(repo, bad)

    def test_accepts_a_nested_relative_path(self, repo: Path):
        assert safe_relative_path(repo, "a/b/c.txt") == (repo / "a" / "b" / "c.txt").resolve()

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
    def test_symlink_out_of_the_tree_is_rejected(self, repo: Path, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (repo / "link.txt").symlink_to(outside)
        with pytest.raises(ValueError):
            safe_relative_path(repo, "link.txt")


class TestOpenFolder:
    def test_spawns_explorer_with_the_directory_as_argv(self, tmp_path: Path, monkeypatch):
        calls = []

        class _Proc:
            pid = 777

        def _popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return _Proc()

        monkeypatch.setattr(git_changes.os, "name", "nt")
        monkeypatch.setattr(git_changes.subprocess, "Popen", _popen)
        monkeypatch.setattr(git_changes.shutil, "which", lambda _n: "C:/Windows/explorer.exe")

        assert open_folder(tmp_path) == 777
        argv, kwargs = calls[0]
        assert argv == ["C:/Windows/explorer.exe", str(tmp_path)]
        assert kwargs["shell"] is False and kwargs["close_fds"] is True
        assert "creationflags" in kwargs

    def test_non_windows_host_raises(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(git_changes.os, "name", "posix")
        with pytest.raises(OSError, match="Windows-only"):
            open_folder(tmp_path)
