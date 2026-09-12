"""Retention window for ``webapp/sessions/`` (issue #902).

Until #902 nothing reaped this directory at all: :mod:`src.audit` appends a
``<session_id>.log`` per session and the session-host writes a
``<session_id>.transcript`` next to it, and both lived forever (measured
2026-09-11: ~94,600 files, 4.9 GB, 99% of it transcripts).

The window itself is **not** set here. It is one field,
``WebappConfig.session_retention_days`` in :mod:`src.webapp_config`, so a
human can find and change it in ``config/webapp_config.json`` without
reading this module; ``0`` keeps everything forever.

:func:`sweep` is the single function that decides and deletes. It is
deliberately conservative, because what it removes is not in git and not
backed up:

- **Age is the file's own mtime** (``lstat``, never following a link) — the
  last time anything was written to it. Not the session id, not the name:
  neither is guaranteed to encode a time, and a transcript that was still
  being appended to last week is not "old" just because its session started
  long ago.
- **An age it cannot establish is ``unknown``, and unknown keeps the file.**
  A stat that raises, a non-finite or non-positive mtime, an mtime in the
  future — each is logged and the file is skipped, never read as "old
  enough". The same holds for the whole sweep: an unreachable session-host
  (so no live list), a sessions directory that can't be listed, or a clock
  that looks wrong stands the sweep down without deleting anything.
- **A live session's files are never selected**, however old they look —
  checked against the session-host's own session list.
- **Nothing outside the directory can be touched.** Only direct children are
  considered (no recursion); only regular files with a ``.log`` or
  ``.transcript`` suffix are candidates; anything that is a symlink or
  junction is skipped; and a sessions directory that is itself a link is
  refused outright rather than followed.

The webapp runs it once a day off the event loop
(``app/webapp/server.py::_session_retention_tick``); the dry-run CLI
``scripts/session_retention.py`` reports the same selection without
deleting.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from src import audit, session_client

logger = logging.getLogger(__name__)

#: The two file kinds the audit trail writes per session.
SESSION_FILE_SUFFIXES = (".log", ".transcript")

#: How far in the future an mtime may sit before it reads as a clock
#: problem rather than ordinary skew between a write and this sweep.
_FUTURE_MTIME_TOLERANCE_SECONDS = 24 * 3600

_SECONDS_PER_DAY = 86400

_PER_FILE_LOG_CAP = 20

STOOD_DOWN_NO_LIVE_LIST = "live session list unknown (session-host unreachable)"
STOOD_DOWN_DIR_ABSENT = "sessions directory does not exist"
STOOD_DOWN_DIR_UNREADABLE = "sessions directory could not be listed"
STOOD_DOWN_DIR_IS_LINK = "sessions directory is a symlink/junction; refusing to follow it"
STOOD_DOWN_CLOCK = (
    "every session file is older than the window — the clock or the directory "
    "looks wrong"
)


@dataclass
class SweepReport:
    """What one :func:`sweep` found and did.

    ``stood_down`` is set when the sweep refused to act at all; in that case
    ``expired`` is empty and nothing was removed. ``unknown`` lists files whose
    age (or nature) couldn't be established — each was kept.
    """

    sessions_dir: Path
    retention_days: int
    dry_run: bool
    stood_down: Optional[str] = None
    scanned: int = 0
    kept_live: int = 0
    kept_young: int = 0
    ignored: int = 0
    expired: List[Path] = field(default_factory=list)
    expired_bytes: int = 0
    removed: int = 0
    unknown: List[Tuple[str, str]] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        if self.stood_down:
            return (
                f"session retention ({self.retention_days}d) stood down for "
                f"{self.sessions_dir}: {self.stood_down}"
            )
        verb = "would remove" if self.dry_run else "removed"
        count = len(self.expired) if self.dry_run else self.removed
        return (
            f"session retention ({self.retention_days}d) {verb} {count} file(s) "
            f"({self.expired_bytes / 1e6:.1f} MB) from {self.sessions_dir}; "
            f"scanned={self.scanned} kept_young={self.kept_young} "
            f"kept_live={self.kept_live} unknown={len(self.unknown)} "
            f"failed={len(self.failed)} ignored={self.ignored}"
        )


def _is_link(path: Path) -> bool:
    """True for a symlink *or* an NTFS junction — either can point out of
    the place it appears to be."""
    return path.is_symlink() or path.is_junction()


def sweep(
    sessions_dir: Path,
    retention_days: int,
    live_session_ids: Optional[Iterable[str]],
    *,
    dry_run: bool,
    now: Optional[float] = None,
) -> SweepReport:
    """Select (and unless ``dry_run``, delete) expired session files.

    ``live_session_ids=None`` means the live list could not be established;
    the sweep stands down rather than guess. ``retention_days`` must be >= 1
    — the "keep forever" switch (``0``) is the caller's to honour by not
    calling at all.
    """
    if retention_days < 1:
        raise ValueError(f"retention_days must be >= 1; got {retention_days}")
    sessions_dir = Path(sessions_dir)
    report = SweepReport(
        sessions_dir=sessions_dir, retention_days=retention_days, dry_run=dry_run
    )
    if live_session_ids is None:
        report.stood_down = STOOD_DOWN_NO_LIVE_LIST
        return report
    live: Set[str] = {str(sid) for sid in live_session_ids if sid}
    now = time.time() if now is None else now
    cutoff = now - retention_days * _SECONDS_PER_DAY

    try:
        if _is_link(sessions_dir):
            report.stood_down = STOOD_DOWN_DIR_IS_LINK
            return report
        entries = list(os.scandir(sessions_dir))
    except FileNotFoundError:
        report.stood_down = STOOD_DOWN_DIR_ABSENT
        return report
    except OSError as exc:
        report.stood_down = f"{STOOD_DOWN_DIR_UNREADABLE}: {exc}"
        return report

    newest_mtime: Optional[float] = None
    candidates: List[Tuple[os.DirEntry, int]] = []
    for entry in entries:
        name = entry.name
        stem, suffix = os.path.splitext(name)
        if suffix not in SESSION_FILE_SUFFIXES:
            report.ignored += 1
            continue
        report.scanned += 1
        try:
            # False for a symlink, a junction and a directory alike, so a
            # link can never be taken for a session file and followed out.
            if not entry.is_file(follow_symlinks=False):
                report.unknown.append(
                    (name, "not a regular file (link, junction or directory)")
                )
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError as exc:
            report.unknown.append((name, f"stat failed: {exc}"))
            continue
        mtime = st.st_mtime
        if not math.isfinite(mtime) or mtime <= 0:
            report.unknown.append((name, f"implausible mtime {mtime!r}"))
            continue
        if mtime > now + _FUTURE_MTIME_TOLERANCE_SECONDS:
            report.unknown.append((name, f"mtime {mtime:.0f} is in the future"))
            continue
        newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)
        if stem in live:
            report.kept_live += 1
        elif mtime < cutoff:
            candidates.append((entry, st.st_size))
        else:
            report.kept_young += 1

    # Clock witness: the audit trail is written continuously, so the newest
    # file in the directory is roughly "now". If even that one is past the
    # window, either nothing has been written for longer than the window or
    # the clock has jumped forward — either way, emptying the directory is
    # not a decision to take unattended.
    if candidates and newest_mtime is not None and newest_mtime < cutoff:
        report.stood_down = STOOD_DOWN_CLOCK
        return report

    for entry, size in candidates:
        path = sessions_dir / entry.name
        report.expired.append(path)
        report.expired_bytes += size
        if dry_run:
            continue
        try:
            os.unlink(path)
            report.removed += 1
        except FileNotFoundError:
            report.removed += 1  # gone already — the outcome we wanted
        except OSError as exc:
            report.failed.append((entry.name, str(exc)))
    return report


def log_report(report: SweepReport) -> None:
    """One summary line, plus a line per kept-unknown or failed file (capped,
    so a directory-wide permission problem can't flood the log daily)."""
    for label, rows in (
        ("kept {} — age unknown: {}", report.unknown),
        ("could not remove {}: {}", report.failed),
    ):
        for name, reason in rows[:_PER_FILE_LOG_CAP]:
            logger.warning("⚠️ session retention " + label.format(name, reason))
        if len(rows) > _PER_FILE_LOG_CAP:
            logger.warning(
                f"⚠️ session retention: {len(rows) - _PER_FILE_LOG_CAP} more "
                f"file(s) like the above not listed"
            )
    if report.stood_down in (None, STOOD_DOWN_DIR_ABSENT):
        logger.info(f"ℹ️ {report.summary()}")
    else:
        logger.warning(f"⚠️ {report.summary()}")


def live_session_ids(port: int) -> Optional[Set[str]]:
    """The session-host's live session ids, or ``None`` when it can't say.

    ``None`` is distinct from an empty set: an empty set means "nothing is
    live", ``None`` means "unknown", and :func:`sweep` stands down on it.
    """
    try:
        sessions = session_client.list_sessions(port)
    except session_client.SessionHostError as exc:
        logger.warning(f"⚠️ session retention: session-host live list unavailable: {exc}")
        return None
    return {str(s.get("session_id")) for s in sessions if s.get("session_id")}


def run_scheduled_sweep(session_host_port: int, retention_days: int) -> SweepReport:
    """The webapp tick's whole cycle: live list, then a real sweep, then log.

    Blocking (HTTP + filesystem) — the caller runs it via
    ``asyncio.to_thread``.
    """
    report = sweep(
        audit.sessions_dir(),
        retention_days,
        live_session_ids(session_host_port),
        dry_run=False,
    )
    log_report(report)
    return report
