"""Issue #668: agent CLIs resolve against the effective PATH, not the
inherited one.

A Windows process inherits its environment block at spawn and never re-reads
the registry, so a CLI installed while the launcher runs stays invisible to
``shutil.which`` — button greyed, and greyed still after a tray restart
whenever that restart is itself launched from a stale-environment shell. The
contract under test: fold the registry values in, never raise, and never let
detection and spawn disagree about where a binary is.
"""

from __future__ import annotations

import os
import sys

import pytest

from src import agents, env_path


@pytest.fixture(autouse=True)
def _no_cache():
    """Each test sees a cold cache — the TTL is an optimization, not
    behaviour under test."""
    env_path._cached = None
    env_path._cached_at = 0.0
    yield
    env_path._cached = None
    env_path._cached_at = 0.0


class TestMerge:
    def test_registry_entries_are_appended_after_inherited(self):
        merged = env_path._merge("C:\\a", "C:\\b", "C:\\c").split(os.pathsep)
        # Inherited first: Windows resolves left to right, so anything this
        # process started with must keep its precedence over a registry entry.
        assert merged == ["C:\\a", "C:\\b", "C:\\c"]

    def test_duplicates_dedupe_case_insensitively(self):
        merged = env_path._merge("C:\\Tools", "c:\\tools", "C:\\Tools\\")
        assert merged == "C:\\Tools"

    def test_blank_and_whitespace_entries_are_dropped(self):
        merged = env_path._merge("C:\\a;;   ;C:\\b")
        assert merged == "C:\\a;C:\\b"

    def test_drive_root_keeps_its_separator(self):
        """``C:\\`` is the root directory; ``C:`` is "the current directory on
        drive C:" — stripping the separator would silently change the lookup."""
        assert env_path._merge("C:\\") == "C:\\"
        assert env_path._merge("C:\\Windows\\") == "C:\\Windows"


class TestEffectivePath:
    def test_non_windows_returns_inherited_unchanged(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        assert env_path.effective_path(refresh=True) == "/usr/bin:/bin"

    @pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
    def test_registry_values_are_folded_in(self, monkeypatch):
        monkeypatch.setenv("PATH", "C:\\inherited")
        monkeypatch.setattr(
            env_path, "_read_registry_path",
            lambda root, key: "C:\\machine" if "SYSTEM" in key else "C:\\user",
        )
        result = env_path.effective_path(refresh=True).split(os.pathsep)
        assert result == ["C:\\inherited", "C:\\machine", "C:\\user"]

    @pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
    def test_registry_failure_degrades_to_inherited(self, monkeypatch):
        """A broken registry read must never raise and never report *fewer*
        paths than today — detection may not get worse than before #668."""
        monkeypatch.setenv("PATH", "C:\\inherited")

        def _boom(root, key):
            raise OSError("registry unavailable")

        monkeypatch.setattr(env_path, "_read_registry_path", _boom)
        assert env_path.effective_path(refresh=True) == "C:\\inherited"

    @pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
    def test_missing_path_value_degrades_to_inherited(self, monkeypatch):
        monkeypatch.setenv("PATH", "C:\\inherited")
        monkeypatch.setattr(env_path, "_read_registry_path", lambda root, key: "")
        assert env_path.effective_path(refresh=True) == "C:\\inherited"

    @pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
    def test_reg_expand_sz_references_are_expanded(self, monkeypatch):
        """An installer writes ``%USERPROFILE%\\.grok\\bin`` as REG_EXPAND_SZ;
        an unexpanded literal would never resolve a binary."""
        import winreg

        monkeypatch.setenv("USERPROFILE", "C:\\Users\\probe")

        class _FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(winreg, "OpenKey", lambda root, key: _FakeKey())
        monkeypatch.setattr(
            winreg, "QueryValueEx",
            lambda key, name: ("%USERPROFILE%\\.grok\\bin", winreg.REG_EXPAND_SZ),
        )
        value = env_path._read_registry_path(winreg.HKEY_CURRENT_USER, "Environment")
        assert value == "C:\\Users\\probe\\.grok\\bin"


class TestDetectionUsesEffectivePath:
    def test_is_installed_resolves_against_the_effective_path(
        self, monkeypatch, tmp_path
    ):
        """The #668 symptom, end to end: a binary reachable only via the
        registry path is detected, where a plain ``shutil.which`` would not
        see it."""
        exe = tmp_path / "claude.exe"
        exe.write_text("", encoding="utf-8")
        # An empty search path finds nothing; the same lookup against a path
        # that contains the binary finds it — i.e. `is_installed` resolves
        # through `effective_path`, not through `os.environ` behind its back.
        monkeypatch.setattr(agents, "effective_path", lambda: "")
        assert agents.is_installed("claude") is False
        monkeypatch.setattr(agents, "effective_path", lambda: str(tmp_path))
        assert agents.is_installed("claude") is True

    def test_unknown_agent_is_never_installed(self):
        assert agents.is_installed("no-such-agent") is False


@pytest.mark.skipif(sys.platform != "win32", reason="session-host is Windows-only")
class TestChildEnvUsesEffectivePath:
    def test_spawned_child_gets_the_same_path_detection_used(self, monkeypatch):
        """Detection and spawn must agree — a button that lights up for a
        launch that then dies with "is not recognized" is the worse bug."""
        from src import session_host

        monkeypatch.setattr(
            session_host, "effective_path", lambda: "C:\\merged;C:\\extra"
        )
        env = session_host.agent_child_env("sid-1", "claude")
        assert env["PATH"] == "C:\\merged;C:\\extra"
        # The marker-scrubbing policy is untouched by #668.
        assert env["APP_LAUNCHER_SESSION_ID"] == "sid-1"
        assert env["APP_LAUNCHER_AGENT"] == "claude"
