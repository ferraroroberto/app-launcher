"""Unit tests for the job-kind registry (``src.jobs_kinds``, issue #70).

Pure ``validate()``/``build_argv()`` checks per kind, plus the registry's
resolution rule — no subprocess, no webapp. Executor-level end-to-end
fires (does the run actually spawn and produce the right run.json /
output.log) live in ``tests/test_run_job_kinds.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.jobs_config import Job
from src.jobs_kinds import KINDS, FILE_KINDS, resolve_kind
from src.jobs_kinds.batch import BatchKind
from src.jobs_kinds.http_check import HttpCheckKind
from src.jobs_kinds.inline_shell import InlineShellKind
from src.jobs_kinds.powershell import POWERSHELL_EXE, PowershellKind
from src.jobs_kinds.python import PythonKind
from src.jobs_kinds.shell_wsl import ShellWslKind


def _job(**overrides) -> Job:
    base = dict(id="j", name="J", script_path="")
    base.update(overrides)
    return Job(**base)


# --------------------------------------------------------------- registry


class TestResolveKind:
    def test_explicit_kind_wins(self):
        job = _job(script_path="C:/x/foo.py", kind="batch")
        assert resolve_kind(job) == "batch"

    def test_unknown_explicit_kind(self):
        job = _job(script_path="C:/x/foo.py", kind="bogus")
        assert resolve_kind(job) == "unknown"

    def test_infers_python_from_suffix(self):
        assert resolve_kind(_job(script_path="C:/x/foo.py")) == "python"

    def test_infers_batch_from_suffix(self):
        assert resolve_kind(_job(script_path="C:/x/foo.bat")) == "batch"

    def test_unrecognized_suffix_with_no_kind_is_unknown(self):
        # Suffix-inference never expands beyond .py/.bat (issue #70) — a
        # .ps1/.sh needs an explicit kind, exactly like job_from_dict enforces.
        assert resolve_kind(_job(script_path="C:/x/foo.ps1")) == "unknown"

    def test_registry_has_all_six_kinds(self):
        assert set(KINDS) == {
            "python", "batch", "powershell", "shell-wsl", "inline-shell", "http-check",
        }
        assert FILE_KINDS == {"python", "batch", "powershell", "shell-wsl"}


# ----------------------------------------------------------------- python


class TestPythonKind:
    def test_build_argv_with_venv(self, tmp_path):
        proj = tmp_path / "proj"
        venv_py = proj / ".venv" / "Scripts" / "python.exe"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("stub")
        script = proj / "sub" / "scrape.py"
        script.parent.mkdir(parents=True)
        script.write_text("# stub")
        job = _job(script_path=str(script))
        argv, cwd, env = PythonKind().build_argv(job, ["--x"], {}, tmp_path)
        assert argv == [str(venv_py), str(script), "--x"]
        assert cwd == proj
        assert env["PYTHONPATH"] == str(proj)

    def test_build_argv_missing_script_raises(self, tmp_path):
        job = _job(script_path=str(tmp_path / "ghost.py"))
        with pytest.raises(OSError):
            PythonKind().build_argv(job, [], {}, tmp_path)

    def test_validate_no_venv_is_warning(self, tmp_path):
        script = tmp_path / "lonely.py"
        script.write_text("# stub")
        problems = PythonKind().validate(_job(script_path=str(script)))
        assert len(problems) == 1
        assert problems[0].level == "warning"

    def test_validate_missing_script_is_silent(self, tmp_path):
        # preflight() itself owns the "script not found" error generically;
        # the kind's own validate() is a no-op when the file doesn't exist.
        job = _job(script_path=str(tmp_path / "ghost.py"))
        assert PythonKind().validate(job) == []


# ------------------------------------------------------------------ batch


class TestBatchKind:
    def test_build_argv(self, tmp_path):
        bat = tmp_path / "demo.bat"
        bat.write_text("@echo off")
        job = _job(script_path=str(bat))
        argv, cwd, env = BatchKind().build_argv(job, ["auto"], {}, tmp_path)
        assert argv == ["cmd.exe", "/c", str(bat), "auto"]
        assert cwd == bat.parent
        assert env == {}

    def test_validate_unresolved_venv_reference_is_warning(self, tmp_path):
        bat = tmp_path / "run.bat"
        bat.write_text(
            '@echo off\r\nC:\\nope\\proj\\.venv\\Scripts\\python.exe app.py\r\n',
            encoding="utf-8",
        )
        problems = BatchKind().validate(_job(script_path=str(bat)))
        assert len(problems) == 1 and problems[0].level == "warning"

    @pytest.mark.parametrize(
        "bad_value", ['"&calc"', "a|b", "a&b", "a^b", "<a", "a>b"]
    )
    def test_build_argv_rejects_cmd_injection_chars(self, tmp_path, bad_value):
        bat = tmp_path / "demo.bat"
        bat.write_text("@echo off")
        job = _job(script_path=str(bat))
        with pytest.raises(ValueError):
            BatchKind().build_argv(job, [bad_value], {}, tmp_path)


# ------------------------------------------------------------- powershell


class TestPowershellKind:
    def test_build_argv(self, tmp_path):
        ps1 = tmp_path / "demo.ps1"
        ps1.write_text("Write-Host hi")
        job = _job(script_path=str(ps1), kind="powershell")
        argv, cwd, env = PowershellKind().build_argv(job, ["-Foo"], {}, tmp_path)
        assert argv == [
            POWERSHELL_EXE, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Foo",
        ]
        assert cwd == ps1.parent

    def test_build_argv_missing_script_raises(self, tmp_path):
        job = _job(script_path=str(tmp_path / "ghost.ps1"), kind="powershell")
        with pytest.raises(OSError):
            PowershellKind().build_argv(job, [], {}, tmp_path)


# -------------------------------------------------------------- shell-wsl


class TestShellWslKind:
    def test_build_argv(self, tmp_path):
        sh = tmp_path / "demo.sh"
        sh.write_text("#!/bin/bash\necho hi\n")
        job = _job(script_path=str(sh), kind="shell-wsl")
        argv, cwd, env = ShellWslKind().build_argv(job, [], {}, tmp_path)
        assert argv == ["wsl", "bash", str(sh)]
        assert cwd == sh.parent

    def test_validate_warns_when_wsl_missing(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        problems = ShellWslKind().validate(_job(kind="shell-wsl"))
        assert len(problems) == 1 and problems[0].level == "warning"

    def test_validate_clean_when_wsl_present(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: r"C:\Windows\System32\wsl.exe")
        assert ShellWslKind().validate(_job(kind="shell-wsl")) == []


# ----------------------------------------------------------- inline-shell


class TestInlineShellKind:
    @pytest.mark.parametrize(
        "ext,expected_prefix",
        [
            (".bat", ["cmd.exe", "/c"]),
            (".ps1", [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]),
            (".sh", ["wsl", "bash"]),
        ],
    )
    def test_build_argv_writes_body_and_delegates(self, tmp_path, ext, expected_prefix):
        job = _job(
            script_path="", kind="inline-shell",
            kind_config={"script_body": "echo hi", "ext": ext},
        )
        argv, cwd, env = InlineShellKind().build_argv(job, ["--tail"], {}, tmp_path)
        temp_script = tmp_path / f"_inline{ext}"
        assert temp_script.is_file()
        assert temp_script.read_text(encoding="utf-8") == "echo hi"
        assert argv == expected_prefix + [str(temp_script), "--tail"]

    def test_build_argv_missing_body_raises(self, tmp_path):
        job = _job(kind="inline-shell", kind_config={"ext": ".bat"})
        with pytest.raises(ValueError):
            InlineShellKind().build_argv(job, [], {}, tmp_path)

    def test_build_argv_bad_ext_raises(self, tmp_path):
        job = _job(
            kind="inline-shell", kind_config={"script_body": "x", "ext": ".exe"},
        )
        with pytest.raises(ValueError):
            InlineShellKind().build_argv(job, [], {}, tmp_path)

    def test_validate_missing_body_is_error(self):
        job = _job(kind="inline-shell", kind_config={"ext": ".bat"})
        problems = InlineShellKind().validate(job)
        assert any(p.level == "error" for p in problems)

    def test_validate_delegates_wsl_warning(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        job = _job(
            kind="inline-shell", kind_config={"script_body": "echo hi", "ext": ".sh"},
        )
        problems = InlineShellKind().validate(job)
        assert any(p.level == "warning" and "wsl.exe" in p.message for p in problems)


# ------------------------------------------------------------- http-check


class TestHttpCheckKind:
    def test_build_argv_defaults(self, tmp_path):
        job = _job(kind="http-check", kind_config={"url": "https://example.com/health"})
        argv, cwd, env = HttpCheckKind().build_argv(job, [], {}, tmp_path)
        assert argv[:3] == [argv[0], "-m", "src.jobs_kinds.http_check_probe"]
        assert "--url" in argv and "https://example.com/health" in argv
        assert "--method" in argv and "GET" in argv
        assert "--expect-status" in argv and "200" in argv

    def test_build_argv_overrides(self, tmp_path):
        job = _job(
            kind="http-check",
            kind_config={
                "url": "https://example.com/health",
                "method": "head",
                "expect_status": 204,
                "timeout": 3,
            },
        )
        argv, _cwd, _env = HttpCheckKind().build_argv(job, [], {}, tmp_path)
        assert "HEAD" in argv
        assert "204" in argv
        assert "3.0" in argv

    def test_build_argv_missing_url_raises(self, tmp_path):
        job = _job(kind="http-check", kind_config={})
        with pytest.raises(ValueError):
            HttpCheckKind().build_argv(job, [], {}, tmp_path)

    def test_validate_missing_url_is_error(self):
        problems = HttpCheckKind().validate(_job(kind="http-check", kind_config={}))
        assert any(p.level == "error" for p in problems)

    def test_validate_bad_scheme_is_error(self):
        job = _job(kind="http-check", kind_config={"url": "example.com/health"})
        problems = HttpCheckKind().validate(job)
        assert any(p.level == "error" for p in problems)

    def test_validate_clean_url_passes(self):
        job = _job(kind="http-check", kind_config={"url": "https://example.com/health"})
        assert HttpCheckKind().validate(job) == []
