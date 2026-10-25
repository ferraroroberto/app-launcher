"""The e2e autoboot webapp writes its runtime data to a per-run folder (#1685).

The gate's disposable webapp mounts the usage log on ``runtime_db_path``,
which with no override resolves the owner's real runtime-data folder, so every
gate run wrote its browser traffic into the file the owner's own visits land
in. These pin the three links without a browser: the env the autoboot builds,
what ``src.runtime_data`` makes of it, and that the webapp spawn carries it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.runtime_data import app_dir_env_var, runtime_db_path
from tests.e2e._autoboot_env import APP_SLUG, runtime_data_env

_CONFTEST = Path(__file__).resolve().parent.parent.parent / "e2e" / "fixtures" / "autoboot.py"


def test_env_carries_the_per_run_folder_under_the_apps_own_variable(tmp_path: Path) -> None:
    assert runtime_data_env(tmp_path) == {app_dir_env_var(APP_SLUG): str(tmp_path)}


def test_env_moves_the_usage_log_into_the_per_run_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in runtime_data_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    db = runtime_db_path(APP_SLUG, "usage.sqlite3", create=True)
    assert db == tmp_path / "usage.sqlite3"


def _webapp_extra_env_calls() -> list[str]:
    """Source of every ``extra_env`` dict handed to ``_spawn`` for uvicorn."""
    tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"))
    spawn_webapp = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_spawn_webapp"
    )
    return [
        ast.unparse(kw.value)
        for call in ast.walk(spawn_webapp)
        if isinstance(call, ast.Call)
        for kw in call.keywords
        if kw.arg == "extra_env"
    ]


def test_autoboot_webapp_spawn_applies_the_runtime_data_env() -> None:
    envs = _webapp_extra_env_calls()
    assert len(envs) == 1, "expected exactly one extra_env in _spawn_webapp"
    assert "runtime_data_env(" in envs[0], (
        "the autoboot webapp is spawned without the runtime-data override; "
        "its usage log would land in the owner's real store (#1685)"
    )
