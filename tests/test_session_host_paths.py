"""``src/session_host_paths.py`` — #635's session-host declared-path scoping.

Exercises the parse and diff helpers directly (not just via `/api/version`),
since the endpoint's own tests mock `_session_host_path_relevance` for
determinism and never exercise these functions' real logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src import session_host_paths

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


class TestDeclaredSessionHostPaths:
    def test_real_claude_md_declares_the_known_session_host_paths(self):
        paths = session_host_paths.declared_session_host_paths(_CLAUDE_MD)
        assert "src/session_host.py" in paths
        assert "app/session_host/" in paths

    def test_missing_file_returns_empty(self, tmp_path):
        assert session_host_paths.declared_session_host_paths(tmp_path / "missing.md") == []

    def test_missing_section_returns_empty(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        md.write_text("## some other heading\n- what/why: `src/other.py`\n", encoding="utf-8")
        assert session_host_paths.declared_session_host_paths(md) == []

    def test_non_path_tokens_are_filtered_out(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        md.write_text(
            "## session-host\n"
            "- what/why: the `:8446` host (`src/session_host.py`) via `cmd /c start`\n"
            "- liveness signal: `GET /api/version`'s `session_host.stale`\n",
            encoding="utf-8",
        )
        paths = session_host_paths.declared_session_host_paths(md)
        assert paths == ["src/session_host.py"]

    def test_not_restarted_by_bullet_is_also_scanned(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        md.write_text(
            "## session-host\n"
            "- what/why: no paths here\n"
            "- not restarted/deployed by: `tray.bat --restart` reclaim of `app/session_host/`\n",
            encoding="utf-8",
        )
        assert session_host_paths.declared_session_host_paths(md) == ["app/session_host/"]


class TestPathsTouchedBetween:
    def _init_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        no_hooks = tmp_path / "no-hooks"
        no_hooks.mkdir()
        run = lambda *args: subprocess.run(  # noqa: E731
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )
        run("init", "-q")
        # This machine's global git config points core.hooksPath at a
        # commit-author allowlist hook — irrelevant to this throwaway,
        # never-pushed scratch repo, so point it at an empty dir instead.
        run("config", "core.hooksPath", str(no_hooks))
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "Test")
        return repo

    def test_returns_true_when_declared_path_touched(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "session_host.py").write_text("v1", encoding="utf-8")
        (repo / "other.py").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, capture_output=True
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

        (repo / "src" / "session_host.py").write_text("v2", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "touch session_host"], cwd=repo,
            check=True, capture_output=True,
        )
        head_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

        result = session_host_paths.paths_touched_between(
            repo, base_sha, head_sha, ["src/session_host.py", "app/session_host/"]
        )
        assert result is True

    def test_returns_false_when_only_unrelated_paths_touched(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "other.py").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, capture_output=True
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

        (repo / "other.py").write_text("v2", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "touch other"], cwd=repo,
            check=True, capture_output=True,
        )
        head_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

        result = session_host_paths.paths_touched_between(
            repo, base_sha, head_sha, ["src/session_host.py", "app/session_host/"]
        )
        assert result is False

    def test_returns_none_for_unresolvable_sha(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "other.py").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, capture_output=True
        )
        result = session_host_paths.paths_touched_between(
            repo, "deadbee", "HEAD", ["src/session_host.py"]
        )
        assert result is None

    def test_returns_none_when_paths_empty(self, tmp_path):
        repo = self._init_repo(tmp_path)
        result = session_host_paths.paths_touched_between(repo, "HEAD", "HEAD", [])
        assert result is None

    def test_returns_none_when_git_raises(self, tmp_path, monkeypatch):
        def _raise(*_args, **_kwargs):
            raise OSError("git not found")
        monkeypatch.setattr(subprocess, "run", _raise)
        result = session_host_paths.paths_touched_between(
            tmp_path, "abc1234", "def5678", ["src/session_host.py"]
        )
        assert result is None
