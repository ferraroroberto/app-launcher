"""Deleting one Life OS conversation everywhere it lives (#1410, shared in #1738).

The body of ``DELETE /api/life-os/skills/{id}/conversations`` lives here so the
route and the zombie cleanup job run **one** deletion routine. The route keeps
the HTTP shell (path jail, auth, audit, response shape) and maps
:class:`ConversationDeleteError` to ``HTTPException``; the job calls
:func:`delete_conversation` directly. With ``stash`` the two filesystem steps
*move* into the backup folder instead of removing, and nothing else differs.

Also here: the live-conversation map both callers consult before deleting
(:func:`live_conversations`), which the router's ``_group_by_conversation`` /
``_live_conversations`` wrap.

Blocking work: callers run it off the event loop.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import board
from src.clients import session as session_client
from src.life_os.index import read_index_entry, reconcile_delete_sync
from src.life_os.title import clear_title_after_delete
from src.life_os.transcript import claude_projects_dir, remove_claude_transcript
from src.life_os.zombie_backup import Stash, build_manifest
from src.session_view.transcript_locate import claude_resume_id
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)


class ConversationDeleteError(Exception):
    """A delete step failed; ``status`` is the HTTP status the route maps it to."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def group_by_conversation(
    cfg: WebappConfig, sessions: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Claude conversation ids a running launcher session is attached to, each to its sessions.

    Two independent signals, either of which makes a conversation live: the
    id a session was *launched* to resume (``--resume <id>`` in its flags, what
    Life OS's Resume spawns), and the id the Board's claim walk assigns it from
    the hook state row (a session that moved to another conversation via
    ``/resume`` or ``/clear``).
    """
    alive = [s for s in sessions if s.get("alive", True)]
    state = board.read_sessions_state(Path(cfg.sessions_state_file))
    live: Dict[str, List[Dict[str, Any]]] = {}
    for sess in alive:
        claimed = board.state_sid_for_session(alive, state["rows"], str(sess.get("session_id")))
        for conversation in (claude_resume_id(sess), claimed):
            if conversation:
                owners = live.setdefault(str(conversation).lower(), [])
                if sess not in owners:
                    owners.append(sess)
    return live


def live_conversations(cfg: WebappConfig) -> Dict[str, Dict[str, Any]]:
    """One session per live conversation id (the last seen).

    Raises ``SessionHostError`` when the host cannot be asked: that is
    "unknown", and the caller must not read it as "nothing is running".
    """
    sessions = session_client.list_sessions(cfg.session_host_port)
    return {sid: owners[-1] for sid, owners in group_by_conversation(cfg, sessions).items()}


def _stash_capture(path: Path, stash: Stash) -> None:
    stash.folder.mkdir(parents=True, exist_ok=True)
    landing = stash.folder / path.name
    if landing.exists():
        raise FileExistsError(f"backup already holds {path.name}")
    shutil.move(str(path), str(landing))


def delete_conversation(
    cfg: WebappConfig, skill_id: str, path: Path, source: Dict[str, Any],
    *, stash: Optional[Stash] = None,
) -> Dict[str, Any]:
    """Remove (or, with ``stash``, back up) one conversation; the caller has vetted it.

    ``path`` is a validated capture of ``skill_id``; ``source`` its
    :func:`src.life_os.history.read_captures` result (header verified). The
    caller also owns the "not live" check. Removes the Claude Code transcript
    (matched by the capture header's **exact** session id, plus its sidecar
    folder), then the capture, then the title, the skill's
    ``conversations/index.json`` row, the ``index.md`` entry and the
    cross-skill search index. The transcript goes *first* on purpose: it is the
    step that can fail on a locked file, and the capture's header is the only
    record of which transcript to remove, so a failure there leaves everything
    intact and retryable instead of orphaning a transcript nobody can find.

    Returns the ``removed`` dict the route replies with. Raises
    :class:`ConversationDeleteError` (status 500) when a filesystem step fails.
    """
    agent, sid = str(source.get("agent", "")), str(source.get("sid", "")).lower()
    dest = None
    if stash is not None:
        # Saved before anything moves: the index row and block are gone after.
        entry = read_index_entry(path)
        stash.write_manifest(path.name, build_manifest(skill_id, path.name, source, entry))
        dest = stash.folder
    try:
        transcript = remove_claude_transcript(claude_projects_dir(), agent, sid, dest=dest)
    except OSError as exc:
        logger.warning("⚠️ Life OS delete: transcript removal failed: %s", exc)
        raise ConversationDeleteError(500, "Nothing else was deleted: the Claude transcript could not be "
                                           "removed (is it open in another program?).") from exc
    if stash is not None:
        stash.write_manifest(path.name, build_manifest(
            skill_id, path.name, source, entry, project=transcript["project"],
            files=transcript["files"], folders=transcript["folders"]))
    try:
        if stash is not None:
            _stash_capture(path, stash)
        else:
            path.unlink()
    except OSError as exc:
        logger.warning("⚠️ Life OS delete: capture removal failed after the transcript went: %s", exc)
        raise ConversationDeleteError(500, "The Claude transcript was removed but the capture could not be: "
                                           "try Delete again.") from exc
    # The title lives in a sidecar keyed by file name, so it would outlive the
    # capture; drop it before the index reconcile so that ends on the rebuild.
    title = clear_title_after_delete(cfg, skill_id, path.name)
    indexes = reconcile_delete_sync(path, cfg)
    removed = {
        "capture": True, "title": title, "transcript": transcript["status"],
        "transcript_files": transcript["files"], "transcript_folders": transcript["folders"],
        **indexes,
    }
    logger.info("ℹ️ Life OS conversation deleted: skill=%s %s", skill_id, removed)
    return removed
