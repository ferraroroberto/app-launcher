"""Unit tests for ``src.jobs_preflight`` (issue #69 PR #1).

Pure-function checks, so no webapp / schtasks scaffolding is needed — we
build :class:`~src.jobs_config.Job` objects directly and inspect the
returned ``Problem`` list.
"""

from __future__ import annotations

from pathlib import Path

from src.jobs_config import Job
from src.jobs_preflight import has_errors, preflight


def _job(script_path: str, args: str = "") -> Job:
    return Job(id="j", name="J", script_path=script_path, args=args)


def _write(path: Path, text: str = "print('ok')\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_clean_py_with_venv_has_no_problems(tmp_path: Path):
    # A .venv at the script's folder resolves the interpreter → clean.
    _write(tmp_path / ".venv" / "Scripts" / "python.exe", "")
    script = _write(tmp_path / "do.py")
    problems = preflight(_job(script))
    assert problems == []
    assert not has_errors(problems)


def test_missing_script_is_error(tmp_path: Path):
    problems = preflight(_job(str(tmp_path / "ghost.py")))
    assert has_errors(problems)
    err = [p for p in problems if p.level == "error"]
    assert err and err[0].field == "script_path"


def test_py_without_venv_is_warning(tmp_path: Path):
    script = _write(tmp_path / "lonely.py")
    problems = preflight(_job(script))
    assert not has_errors(problems)
    warns = [p for p in problems if p.level == "warning"]
    assert warns and warns[0].field == "script_path"
    assert "sys.executable" in warns[0].message


def test_bat_with_unresolved_venv_reference_is_warning(tmp_path: Path):
    # A .bat that names a .venv interpreter which doesn't exist → warning.
    bat = tmp_path / "run.bat"
    bat.write_text(
        '@echo off\r\n'
        'C:\\nope\\proj\\.venv\\Scripts\\python.exe app.py\r\n',
        encoding="utf-8",
    )
    problems = preflight(_job(str(bat)))
    assert not has_errors(problems)
    assert any(p.level == "warning" and ".venv" in p.message for p in problems)


def test_bat_without_venv_reference_is_clean(tmp_path: Path):
    bat = tmp_path / "plain.bat"
    bat.write_text("@echo off\r\necho hi\r\n", encoding="utf-8")
    assert preflight(_job(str(bat))) == []


def test_unbalanced_args_quote_is_error(tmp_path: Path):
    # .bat so the venv check stays out of the way; bad quote on args.
    bat = tmp_path / "x.bat"
    bat.write_text("@echo off\r\n", encoding="utf-8")
    problems = preflight(_job(str(bat), args='ok "dangling'))
    assert has_errors(problems)
    assert any(p.field == "args" for p in problems)


def test_well_formed_quoted_args_pass(tmp_path: Path):
    bat = tmp_path / "y.bat"
    bat.write_text("@echo off\r\n", encoding="utf-8")
    problems = preflight(_job(str(bat), args='--name "two words" --flag'))
    assert [p for p in problems if p.field == "args"] == []
