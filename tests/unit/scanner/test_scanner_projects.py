"""src.scanner — directory-based Claude Code discovery (issue #44)."""

from __future__ import annotations

from pathlib import Path

from src.scanner import dir_ignored, github_repo_url, scan_project_dirs


def _make_git_repo(project_dir: Path, origin_url: str | None) -> None:
    """Create a minimal ``.git/config`` under ``project_dir``."""
    git_dir = project_dir / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    lines = ['[core]', '\trepositoryformatversion = 0']
    if origin_url is not None:
        lines += ['[remote "origin"]', f'\turl = {origin_url}']
    (git_dir / "config").write_text("\n".join(lines) + "\n", encoding="utf-8")


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

    def test_name_is_raw_folder_name(self, tmp_path: Path):
        """The Coding tab shows the bare on-disk name — no prettification
        (issue #45). 'My Project' stays 'My Project', not 'My Project'
        title-cased or slugified."""
        (tmp_path / "weird_folder-Name").mkdir()
        found = scan_project_dirs(tmp_path)
        assert found[0].name == "weird_folder-Name"


class TestGithubRepoUrl:
    def test_https_remote_strips_dot_git(self, tmp_path: Path):
        _make_git_repo(tmp_path, "https://github.com/owner/repo.git")
        assert github_repo_url(tmp_path) == "https://github.com/owner/repo"

    def test_https_remote_without_dot_git(self, tmp_path: Path):
        _make_git_repo(tmp_path, "https://github.com/owner/repo")
        assert github_repo_url(tmp_path) == "https://github.com/owner/repo"

    def test_scp_ssh_remote(self, tmp_path: Path):
        _make_git_repo(tmp_path, "git@github.com:owner/repo.git")
        assert github_repo_url(tmp_path) == "https://github.com/owner/repo"

    def test_ssh_protocol_remote(self, tmp_path: Path):
        _make_git_repo(tmp_path, "ssh://git@github.com/owner/repo.git")
        assert github_repo_url(tmp_path) == "https://github.com/owner/repo"

    def test_non_github_host_returns_none(self, tmp_path: Path):
        _make_git_repo(tmp_path, "git@gitlab.com:owner/repo.git")
        assert github_repo_url(tmp_path) is None

    def test_no_origin_remote_returns_none(self, tmp_path: Path):
        _make_git_repo(tmp_path, None)
        assert github_repo_url(tmp_path) is None

    def test_no_git_dir_returns_none(self, tmp_path: Path):
        assert github_repo_url(tmp_path) is None

    def test_tolerates_duplicate_keys(self, tmp_path: Path):
        """Git config allows a key to repeat within a section (multivar);
        VS Code writes duplicate `vscode-merge-base` entries. The parser
        must not choke on those — configparser's strict mode would."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            '[branch "main"]\n'
            "\tvscode-merge-base = origin/main\n"
            "\tvscode-merge-base = origin/main\n"
            '[remote "origin"]\n'
            "\turl = https://github.com/owner/repo.git\n",
            encoding="utf-8",
        )
        assert github_repo_url(tmp_path) == "https://github.com/owner/repo"
