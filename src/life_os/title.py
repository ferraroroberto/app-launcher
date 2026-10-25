"""Running fleet-config's conversation-title writer for one Life OS capture.

Moved out of ``app/webapp/routers/life_os_conversations.py`` (#1738) so the
zombie cleanup job, which shares the Delete code path with that route, can clear
a deleted capture's title and put one back on restore without importing a
router. The route's rename and the Delete both still go through
:func:`run_title_cli`; nothing about the writer's contract changed.

Blocking work: callers run it off the event loop.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Optional

from src.life_os.index import title_cli
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

# One title write at a time: the writer's contract is that two concurrent writes
# to one folder can lose one, and a threading lock (taken in the worker thread,
# not on the loop) also covers Delete's clear.
TITLE_TIMEOUT_S = 30
_title_write_lock = threading.Lock()


def run_title_cli(
    cfg: WebappConfig, skill_id: str, file: str, title: Optional[str],
) -> subprocess.CompletedProcess:
    """Run fleet-config's title writer for one capture; ``title`` empty/``None`` clears.

    ``--title=<text>`` (one argv element), never ``--title <text>``: argparse
    reads a title starting with ``-`` as a missing value. Serialised with
    :data:`_title_write_lock`. Raises ``FileNotFoundError`` when the writer is
    not installed, ``OSError`` / ``subprocess.SubprocessError`` when it did
    not run; the caller owns what a non-zero exit means.
    """
    cli = title_cli(cfg)
    if cli is None:
        raise FileNotFoundError("conversation_title.py")
    argv = [*cli, "--cwd", str(cfg.life_os_dir), "--skill", skill_id, "--file", file,
            f"--title={title}" if title else "--clear"]
    with _title_write_lock:
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TITLE_TIMEOUT_S, creationflags=NO_WINDOW,
        )


def clear_title_after_delete(cfg: WebappConfig, skill_id: str, file: str) -> str:
    """Drop a deleted capture's title: ``cleared`` | ``unavailable``.

    Best-effort like the other index reconciles - the capture is already gone,
    so a writer that is missing or fails is logged, not an error to the phone.
    A stale entry in the sidecar is inert (the writer only applies titles to
    captures that exist).
    """
    try:
        proc = run_title_cli(cfg, skill_id, file, None)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("⚠️ Life OS delete: could not clear the title: %s", exc)
        return "unavailable"
    if proc.returncode != 0:
        logger.warning("⚠️ Life OS delete: title clear exited %s: %s",
                       proc.returncode, (proc.stderr or "").strip()[:300])
        return "unavailable"
    return "cleared"
