"""``http-check`` job-kind (issue #70) — a synthetic kind with no script on
disk: polls a URL and succeeds/fails on the response status.

Rather than teach the executor an in-process code path, ``build_argv``
shells out to :mod:`src.jobs_kinds.http_check_probe` via ``python -m`` —
this reuses the *entire* existing executor machinery (``subprocess.Popen``,
the ``visible``-job console tee, the resource sampler, exit-code capture,
``output.log``) for free.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

from src.jobs_config import PROJECT_ROOT, Job
from src.jobs_kinds.base import Problem

DEFAULT_METHOD = "GET"
DEFAULT_EXPECT_STATUS = 200
DEFAULT_TIMEOUT_SECONDS = 10.0


class HttpCheckKind:
    name = "http-check"

    def validate(self, job: Job) -> List[Problem]:
        cfg = job.kind_config or {}
        url = cfg.get("url")
        problems: List[Problem] = []
        if not url or not str(url).strip():
            problems.append(
                Problem(
                    level="error",
                    field="kind_config",
                    message="http-check requires a non-empty url.",
                )
            )
        elif not str(url).lower().startswith(("http://", "https://")):
            problems.append(
                Problem(
                    level="error",
                    field="kind_config",
                    message=f"http-check url must start with http:// or https://, got {url!r}.",
                )
            )
        return problems

    def build_argv(
        self, job: Job, tail: List[str], param_env: Dict[str, str], run_dir: Path
    ) -> Tuple[List[str], Path, Dict[str, str]]:
        cfg = job.kind_config or {}
        url = cfg.get("url")
        if not url or not str(url).strip():
            raise ValueError("http-check job has no url")
        method = str(cfg.get("method") or DEFAULT_METHOD).upper()
        expect_status = int(cfg.get("expect_status") or DEFAULT_EXPECT_STATUS)
        timeout = float(cfg.get("timeout") or DEFAULT_TIMEOUT_SECONDS)

        argv = [
            sys.executable,
            "-m",
            "src.jobs_kinds.http_check_probe",
            "--url",
            str(url),
            "--method",
            method,
            "--expect-status",
            str(expect_status),
            "--timeout",
            str(timeout),
        ]
        return argv, PROJECT_ROOT, dict(param_env)
