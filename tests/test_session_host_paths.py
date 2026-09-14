"""``src/session_host_paths.py`` — #635's session-host declared-path scoping.

Exercises the parse and diff helpers directly (not just via `/api/version`),
since the endpoint's own tests mock `_session_host_path_relevance` for
determinism and never exercise these functions' real logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src import session_host_paths

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"


class TestDeclaredSessionHostPaths:
    def test_real_claude_md_declares_the_known_session_host_paths(self):
        paths = session_host_paths.declared_session_host_paths(_CLAUDE_MD)
        assert "src/session_host.py" in paths
        assert "app/session_host/" in paths

    def test_declaration_covers_the_whole_import_closure(self):
        """The declaration must name every module the session-host loads.

        #832 pinned this for the ``session_host_*`` siblings only, by grepping
        `src/session_host.py`'s own imports for names starting with
        ``session_host`` — a hand-picked shape that #923 found still let
        `src/audit.py` (imported by `session_host.py` to write transcripts,
        named nothing like the rest) go undeclared. An undeclared module makes
        `_touched_by` return a confident False for a change that genuinely
        needs a `:8446` restart, and nobody finds out.

        So the check is now against the real transitive closure, with no name
        filter: a module reachable from the server entry point that isn't
        declared fails here, whatever it's called.
        """
        closure = session_host_paths.session_host_import_closure(_REPO_ROOT)
        assert closure is not None, "session-host import closure could not be computed"
        assert "app/session_host/server.py" in closure, "sanity: entry point missing"
        assert "src/session_host.py" in closure, "sanity: closure did not follow imports"

        declared = session_host_paths.declared_session_host_paths(_CLAUDE_MD)
        undeclared = [
            path
            for path in closure
            if not session_host_paths._touched_by([path], declared)
        ]
        assert not undeclared, (
            f"{undeclared} are imported by the session-host but not declared in "
            "CLAUDE.md's ## session-host block. Add them there (see its 'path "
            "list' bullet) and as a full-tier path rule in .fleet.toml's [e2e] table."
        )

    def test_declaration_names_nothing_the_session_host_does_not_load(self):
        """The inverse guard: no declared path outside the closure.

        `stale_relevant` is only worth reading if it stays narrow — declaring
        a module the host never loads would flip it `true` after merges that
        need no restart, which is the noise #635 removed.
        """
        closure = session_host_paths.session_host_import_closure(_REPO_ROOT)
        assert closure is not None
        stray = [
            path
            for path in session_host_paths.declared_session_host_paths(_CLAUDE_MD)
            if not any(session_host_paths._touched_by([f], [path]) for f in closure)
        ]
        assert not stray, (
            f"{stray} are declared session-host paths but nothing in the "
            "session-host's import closure lives there"
        )

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


class TestSessionHostImportClosure:
    """``session_host_import_closure`` — #923's drift check on the declaration."""

    def _tree(self, tmp_path: Path) -> Path:
        (tmp_path / "app" / "session_host").mkdir(parents=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "app" / "session_host" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
        return tmp_path

    def test_follows_transitive_first_party_imports(self, tmp_path):
        repo = self._tree(tmp_path)
        (repo / "app" / "session_host" / "server.py").write_text(
            "import json\nfrom src.host import Manager\n", encoding="utf-8"
        )
        (repo / "src" / "host.py").write_text(
            "from src.deep import thing\n", encoding="utf-8"
        )
        (repo / "src" / "deep.py").write_text("thing = 1\n", encoding="utf-8")
        (repo / "src" / "unrelated.py").write_text("x = 1\n", encoding="utf-8")

        closure = session_host_paths.session_host_import_closure(repo)
        assert closure == [
            "app/__init__.py",
            "app/session_host/__init__.py",
            "app/session_host/server.py",
            "src/__init__.py",
            "src/deep.py",
            "src/host.py",
        ]

    def test_follows_imports_nested_in_functions(self, tmp_path):
        """A lazy import inside a function still loads the module at runtime."""
        repo = self._tree(tmp_path)
        (repo / "app" / "session_host" / "server.py").write_text(
            "def run():\n    from src.late import go\n    return go\n", encoding="utf-8"
        )
        (repo / "src" / "late.py").write_text("go = 1\n", encoding="utf-8")
        closure = session_host_paths.session_host_import_closure(repo)
        assert "src/late.py" in closure

    def test_resolves_relative_imports(self, tmp_path):
        repo = self._tree(tmp_path)
        (repo / "app" / "session_host" / "server.py").write_text(
            "from .helper import h\n", encoding="utf-8"
        )
        (repo / "app" / "session_host" / "helper.py").write_text("h = 1\n", encoding="utf-8")
        closure = session_host_paths.session_host_import_closure(repo)
        assert "app/session_host/helper.py" in closure

    def test_survives_an_import_cycle(self, tmp_path):
        repo = self._tree(tmp_path)
        (repo / "app" / "session_host" / "server.py").write_text(
            "from src.a import x\n", encoding="utf-8"
        )
        (repo / "src" / "a.py").write_text("from src.b import y\n", encoding="utf-8")
        (repo / "src" / "b.py").write_text("from src.a import x\n", encoding="utf-8")
        closure = session_host_paths.session_host_import_closure(repo)
        assert "src/a.py" in closure and "src/b.py" in closure

    def test_returns_none_when_entry_module_is_missing(self, tmp_path):
        assert session_host_paths.session_host_import_closure(tmp_path) is None

    def test_returns_none_rather_than_a_partial_closure_on_a_parse_error(self, tmp_path):
        """A file it can't parse makes the whole closure unknown.

        Returning what it managed to walk would under-declare silently — the
        exact shape this check exists to catch (#923's "unknown is its own
        state" constraint).
        """
        repo = self._tree(tmp_path)
        (repo / "app" / "session_host" / "server.py").write_text(
            "from src.broken import x\n", encoding="utf-8"
        )
        (repo / "src" / "broken.py").write_text("def (:\n", encoding="utf-8")
        assert session_host_paths.session_host_import_closure(repo) is None


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


class TestShaContainsCommit:
    """#967 reopen: whether a running session-host's loaded sha already has a
    feature commit — the fact the webapp needs to say "restart needed"
    instead of forwarding the stale host's bare HTTP 500."""

    def _commit(self, repo: Path, name: str) -> str:
        (repo / f"{name}.txt").write_text(name, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", name], cwd=repo, check=True, capture_output=True
        )
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def test_true_when_sha_descends_from_commit(self, tmp_path):
        repo = TestPathsTouchedBetween()._init_repo(tmp_path)
        feature = self._commit(repo, "feature")
        later = self._commit(repo, "later")
        assert session_host_paths.sha_contains_commit(repo, later, feature) is True
        assert session_host_paths.sha_contains_commit(repo, feature, feature) is True

    def test_false_when_sha_predates_commit(self, tmp_path):
        repo = TestPathsTouchedBetween()._init_repo(tmp_path)
        old = self._commit(repo, "old")
        feature = self._commit(repo, "feature")
        assert session_host_paths.sha_contains_commit(repo, old, feature) is False

    def test_none_for_unresolvable_sha(self, tmp_path):
        # Unknown is its own state — never a confident "predates".
        repo = TestPathsTouchedBetween()._init_repo(tmp_path)
        feature = self._commit(repo, "feature")
        assert session_host_paths.sha_contains_commit(repo, "deadbee", feature) is None
        assert session_host_paths.sha_contains_commit(repo, feature, "deadbee") is None
