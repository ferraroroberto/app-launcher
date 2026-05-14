"""Audit logging for the interactive terminal.

Two tiers, both under ``webapp/`` (gitignored runtime state):

- ``webapp/terminal_audit.log`` — one cross-session log: passkey enroll /
  success / failure, session start / stop, the device + client IP behind
  each action.
- ``webapp/sessions/<session_id>.log`` — per-session: every input chunk
  sent from the phone, image uploads, lifecycle.

The full terminal *output* transcript is written separately by the
session-host (it owns the output stream) to
``webapp/sessions/<session_id>.transcript``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AUDIT_DIR = PROJECT_ROOT / "webapp"
_SESSIONS_DIR = _AUDIT_DIR / "sessions"
_AUDIT_LOG = _AUDIT_DIR / "terminal_audit.log"

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("launcher.terminal_audit")
_handler_ready = False


def _ensure_handler() -> None:
    global _handler_ready
    if _handler_ready:
        return
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        if not any(
            isinstance(h, logging.FileHandler)
            and Path(h.baseFilename).resolve() == _AUDIT_LOG.resolve()
            for h in _audit_logger.handlers
        ):
            fh = logging.FileHandler(_AUDIT_LOG, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            _audit_logger.addHandler(fh)
            _audit_logger.setLevel(logging.INFO)
        _handler_ready = True
    except OSError as exc:  # pragma: no cover
        logger.warning(f"⚠️  Could not open {_AUDIT_LOG}: {exc}")


def _fmt_fields(fields: dict) -> str:
    return " ".join(f"{k}={v!r}" for k, v in fields.items() if v is not None)


def audit_event(event: str, **fields: Any) -> None:
    """Append one line to the cross-session terminal audit log."""
    _ensure_handler()
    _audit_logger.info(f"[{event}] {_fmt_fields(fields)}".rstrip())


def session_log(session_id: str, event: str, **fields: Any) -> None:
    """Append one line to a session's own log file."""
    try:
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        line = f"{stamp} [{event}] {_fmt_fields(fields)}".rstrip()
        with (_SESSIONS_DIR / f"{session_id}.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(line + "\n")
    except OSError as exc:  # pragma: no cover
        logger.debug(f"session_log write failed: {exc}")


def session_input(session_id: str, data: str) -> None:
    """Record an input chunk sent to a session (kept verbatim for the audit)."""
    if not data:
        return
    try:
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with (_SESSIONS_DIR / f"{session_id}.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(f"{stamp} [input] {data!r}\n")
    except OSError as exc:  # pragma: no cover
        logger.debug(f"session_input write failed: {exc}")


def transcript_path(session_id: str) -> Path:
    """Where the session-host writes a session's full output transcript."""
    return _SESSIONS_DIR / f"{session_id}.transcript"
