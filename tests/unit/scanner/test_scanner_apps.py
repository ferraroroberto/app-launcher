"""src.scanner — bat-based Apps tab discovery (issue #77)."""

from __future__ import annotations

from pathlib import Path

from src.scanner import (
    KIND_STREAMLIT,
    KIND_TRAY,
    KIND_TUNNEL,
    KIND_WEBAPP,
    _wrapper_scripts,
    classify_bat,
    pretty_folder_name,
    scan_app_bats,
)


def _write_bat(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestClassifyBat:
    def test_tray(self, tmp_path: Path):
        bat = _write_bat(
            tmp_path / "tray.bat",
            'set "TRAY_PS=%USERPROFILE%/.claude/tray/tray_lifecycle.ps1"',
        )
        assert classify_bat(bat) == KIND_TRAY

    def test_tray_requires_exact_stem(self, tmp_path: Path):
        """A differently-named bat that happens to reference
        tray_lifecycle.ps1 must NOT classify as tray — the fixed ``tray.bat``
        filename is part of the shared fleet convention, not incidental."""
        bat = _write_bat(
            tmp_path / "start_tray.bat", "call tray_lifecycle.ps1 launch"
        )
        assert classify_bat(bat) is None

    def test_tray_requires_lifecycle_marker(self, tmp_path: Path):
        """A bat literally named tray.bat that doesn't reference the shared
        template (e.g. some unrelated per-project script) isn't misclassified."""
        bat = _write_bat(tmp_path / "tray.bat", "echo not a real fleet tray")
        assert classify_bat(bat) is None

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


class TestClassifyBatWrapperScript:
    """A bat that hands off to a ``.py`` wrapper is classified by one level of
    that script's text; the script is read, never run."""

    def test_streamlit_via_cli_module(self, tmp_path: Path):
        _write_bat(
            tmp_path / "scripts" / "run_streamlit.py",
            "from streamlit.web import cli\ncli.main()\n",
        )
        bat = _write_bat(
            tmp_path / "launch_app.bat",
            r".venv\Scripts\python.exe scripts\run_streamlit.py",
        )
        assert classify_bat(bat) == KIND_STREAMLIT

    def test_streamlit_via_subprocess_module_run(self, tmp_path: Path):
        _write_bat(
            tmp_path / "scripts" / "run_streamlit.py",
            'subprocess.run([sys.executable, "-m", "streamlit", "run", "app.py"])\n',
        )
        bat = _write_bat(
            tmp_path / "launch_app.bat",
            r"%~dp0.venv\Scripts\python.exe %~dp0scripts\run_streamlit.py",
        )
        assert classify_bat(bat) == KIND_STREAMLIT

    def test_streamlit_via_bootstrap_run(self, tmp_path: Path):
        _write_bat(
            tmp_path / "go.py",
            "from streamlit.web import bootstrap\nbootstrap.run('app.py', False, [], {})\n",
        )
        bat = _write_bat(tmp_path / "launch_app.bat", "python go.py")
        assert classify_bat(bat) == KIND_STREAMLIT

    def test_webapp_via_uvicorn_run(self, tmp_path: Path):
        _write_bat(
            tmp_path / "scripts" / "serve.py",
            "import uvicorn\nuvicorn.run(app, port=8000)\n",
        )
        bat = _write_bat(tmp_path / "launch_app.bat", r"python scripts\serve.py")
        assert classify_bat(bat) == KIND_WEBAPP

    def test_backslash_path_is_joined_with_forward_slashes(
        self, tmp_path: Path, monkeypatch
    ):
        # POSIX treats a backslash as a file-name character, so a bat's
        # ``scripts\run.py`` must reach the filesystem as ``scripts/run.py``.
        # Recording what is joined proves that on any OS.
        script = _write_bat(tmp_path / "scripts" / "run.py", "import uvicorn\n")
        bat = _write_bat(tmp_path / "launch_app.bat", r"python scripts\run.py")
        joined: list[str] = []
        path_type = type(tmp_path)
        original = path_type.__truediv__

        def record(self, other):
            joined.append(str(other))
            return original(self, other)

        monkeypatch.setattr(path_type, "__truediv__", record)
        found = _wrapper_scripts(bat, bat.read_text(encoding="utf-8"))
        monkeypatch.undo()

        assert found == [script.resolve()]
        assert joined and all("\\" not in part for part in joined)

    def test_script_without_signature_is_none(self, tmp_path: Path):
        _write_bat(tmp_path / "tool.py", "print('hello')\n")
        bat = _write_bat(tmp_path / "launch_app.bat", "python tool.py")
        assert classify_bat(bat) is None

    def test_missing_script_is_none(self, tmp_path: Path):
        bat = _write_bat(tmp_path / "launch_app.bat", r"python scripts\gone.py")
        assert classify_bat(bat) is None

    def test_script_outside_the_bat_folder_is_not_followed(self, tmp_path: Path):
        _write_bat(tmp_path / "elsewhere" / "run.py", "streamlit run app.py\n")
        bat = _write_bat(
            tmp_path / "proj" / "launch_app.bat", r"python ..\elsewhere\run.py"
        )
        assert classify_bat(bat) is None

    def test_absolute_script_path_is_not_followed(self, tmp_path: Path):
        outside = _write_bat(tmp_path / "elsewhere" / "run.py", "streamlit run a.py\n")
        bat = _write_bat(tmp_path / "proj" / "launch_app.bat", f'python "{outside}"')
        assert classify_bat(bat) is None

    def test_bat_text_signature_wins_without_reading_scripts(
        self, tmp_path: Path, monkeypatch
    ):
        _write_bat(tmp_path / "w.py", "uvicorn.run(app)\n")
        bat = _write_bat(tmp_path / "x.bat", "streamlit run app.py\npython w.py")
        import src.scanner as scanner

        def boom(*_a, **_k):
            raise AssertionError("script must not be read")

        monkeypatch.setattr(scanner, "_read_script_text", boom)
        assert classify_bat(bat) == KIND_STREAMLIT

    def test_script_is_never_executed_or_imported(self, tmp_path: Path):
        marker = tmp_path / "ran.txt"
        _write_bat(
            tmp_path / "w.py",
            f"open({str(marker)!r}, 'w').write('x')\n# streamlit run app.py\n",
        )
        bat = _write_bat(tmp_path / "launch_app.bat", "python w.py")
        assert classify_bat(bat) == KIND_STREAMLIT
        assert not marker.exists()


class TestScanAppBats:
    def test_lists_wrapper_launched_app(self, tmp_path: Path):
        _write_bat(
            tmp_path / "alpha" / "scripts" / "run_streamlit.py",
            "from streamlit.web import cli\n",
        )
        _write_bat(
            tmp_path / "alpha" / "launch_app.bat",
            r".venv\Scripts\python.exe scripts\run_streamlit.py",
        )
        found = scan_app_bats(tmp_path)
        assert [(p.parent.name, k) for p, k in found] == [("alpha", KIND_STREAMLIT)]

    def test_logs_one_info_line_per_unclassified_bat(
        self, tmp_path: Path, caplog
    ):
        _write_bat(tmp_path / "alpha" / "tool.py", "print('hi')\n")
        _write_bat(tmp_path / "alpha" / "launch_app.bat", "python tool.py")
        with caplog.at_level("INFO", logger="src.scanner"):
            found = scan_app_bats(tmp_path)
        assert found == []
        lines = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(lines) == 1
        assert str(Path("alpha") / "launch_app.bat") in lines[0].getMessage()
        assert str(tmp_path) not in lines[0].getMessage()

    def test_classified_bat_logs_nothing(self, tmp_path: Path, caplog):
        _write_bat(tmp_path / "alpha" / "run.bat", "streamlit run app.py")
        with caplog.at_level("INFO", logger="src.scanner"):
            scan_app_bats(tmp_path)
        assert [r for r in caplog.records if r.levelname == "INFO"] == []

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


class TestPrettyFolderName:
    def test_sentence_case_not_title_case(self):
        assert pretty_folder_name(Path("mass-html_to-markdown")) == "Mass html to markdown"

    def test_single_word_is_capitalised(self):
        assert pretty_folder_name(Path("pvgis")) == "Pvgis"
