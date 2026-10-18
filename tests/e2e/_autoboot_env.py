"""The runtime-data isolation the e2e autoboot webapp is spawned with (#1685).

The disposable webapp mounts the usage log on ``runtime_db_path("app-launcher",
...)``, which falls back to the real ``C:\\sqlite\\app-launcher`` when no
override is set, so every gate run wrote its browser traffic into the file the
owner's own visits land in. Kept out of ``conftest.py`` so a non-browser test
can pin the env without importing the browser fixtures.
"""
from __future__ import annotations

from pathlib import Path

from src.runtime_data import app_dir_env_var

APP_SLUG = "app-launcher"


def runtime_data_env(runtime_data_dir: Path) -> dict[str, str]:
    """Env that points every ``src.runtime_data`` lookup at a per-run folder."""
    return {app_dir_env_var(APP_SLUG): str(runtime_data_dir)}
