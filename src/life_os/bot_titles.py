"""A stable owner title for a Life OS bot chat's conversation.

A Telegram bot chat is one long-running conversation, and its history row shows
the digest topic, which the indexer regenerates on every refresh. This gives the
row a fixed name per skill instead, written through the same owner-title writer
as a rename, so a later rename of the row always wins (only an untitled row is
ever named here).

A conversation is a bot chat when a live session holding it carries a
``telegram:<profile>`` label; that label is the one marker the session host
stamps at spawn. Normal sessions, launcher-started ones included, carry none.

Blocking work: callers run it off the event loop.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from src.launching.channel_profiles import label_profile_id
from src.life_os.index import read_index_entry
from src.life_os.title import run_title_cli
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

BOT_TITLE_PREFIX = "Telegram bot - "


def bot_title(skill_id: str) -> str:
    """The fixed owner title of a bot chat in ``skill_id``."""
    return f"{BOT_TITLE_PREFIX}{skill_id}"


def bot_conversation_ids(groups: Dict[str, List[Dict[str, Any]]]) -> Set[str]:
    """Conversation ids (lower-case) that a ``telegram:*`` session holds.

    ``groups`` is :func:`src.life_os.delete.group_by_conversation`'s map.
    """
    return {conversation for conversation, owners in groups.items()
            if any(label_profile_id(owner.get("label")) for owner in owners)}


def pending_bot_rows(
    rows: Iterable[Dict[str, Any]], bot_ids: Set[str],
) -> List[Dict[str, Any]]:
    """Rows of a bot conversation that have no owner title and a readable capture."""
    return [row for row in rows
            if not row.get("title") and row.get("path") and row.get("skill")
            and str(row.get("sid") or "").lower() in bot_ids]


def _has_owner_title(path: Path) -> bool:
    """Whether the index row already carries a title; unreadable counts as titled.

    Re-read just before the write so an owner rename that landed since the list
    was built is never overwritten; when the index cannot be read, leaving the
    row alone is the safe answer.
    """
    row = read_index_entry(path)["index_json_row"]
    return row is None or bool(row.get("title"))


def apply_bot_titles(
    cfg: WebappConfig, rows: List[Dict[str, Any]], bot_ids: Set[str],
) -> int:
    """Write the fixed title onto each untitled bot row and mirror it into ``rows``.

    Best-effort: a missing or failing writer is logged and leaves that row on
    its digest topic. Returns the number of rows titled.
    """
    titled = 0
    for row in pending_bot_rows(rows, bot_ids):
        path = Path(cfg.life_os_dir) / row["path"]
        title = bot_title(row["skill"])
        try:
            if _has_owner_title(path):
                continue
            proc = run_title_cli(cfg, row["skill"], row["file"], title)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("⚠️ Life OS bot chat title not written: %s", exc)
            continue
        if proc.returncode != 0:
            logger.warning("⚠️ Life OS bot chat title writer exited %s: %s",
                           proc.returncode, (proc.stderr or "").strip()[:300])
            continue
        row["title"] = title
        titled += 1
    if titled:
        logger.info("ℹ️ Life OS bot chat titled: count=%d", titled)
    return titled
