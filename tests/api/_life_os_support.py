"""Constants and builders shared by the Life OS API modules."""

from __future__ import annotations

import subprocess


# A canonical session id — the only shape the launch route accepts, because
# the value reaches claude's command line.
RESUMABLE_SID = "e70b4cb1-9f3d-4a21-8c55-2b7d19a4f6e0"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["python"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ------------------------------- last used + resume the latest (#1503)
OTHER_SID ="4c2f8a10-6b3e-4d71-9a05-7e81c3d2b9f4"
