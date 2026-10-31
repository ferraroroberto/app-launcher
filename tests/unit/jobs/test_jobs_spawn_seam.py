"""``src.jobs.spawn`` is the seam between "run a job" and "schedule a job"."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# Modules that run a job but never register one with Task Scheduler.
RUNNERS = [
    ROOT / "src" / "jobs" / "queue.py",
    ROOT / "src" / "jobs" / "reap.py",
    *sorted((ROOT / "src" / "jobs_kinds").glob("*.py")),
]


def _imported_modules(path: Path) -> set[str]:
    sources: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            sources.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "src.jobs":
                sources.update(f"src.jobs.{alias.name}" for alias in node.names)
            elif node.module:
                sources.add(node.module)
    return sources


def test_job_runners_do_not_import_the_task_scheduler_client():
    offenders = {
        str(path.relative_to(ROOT)): sorted(
            s for s in _imported_modules(path) if s == "src.jobs.schtasks"
        )
        for path in RUNNERS
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, (
        "spawning and venv lookup live in src.jobs.spawn; importing the "
        f"Task Scheduler client for them rebuilds the old coupling: {offenders}"
    )
