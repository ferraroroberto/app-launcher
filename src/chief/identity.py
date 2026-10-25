"""Which live session is the fleet chief, with no UI import.

The Board router, the chief doorbell and the job-failed event all ask the
same question of a session-host list; the answer lives here so ``src`` code
can ask it without importing the router.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from src import board

logger = logging.getLogger(__name__)

CHIEF_LABEL = "chief"
CHIEF_COMMAND = "/chief"
CHIEF_TITLE = "chief"
# The directory-name grain every "is this fleet-config?" check in this repo
# uses (the Board state's project fallback, the state-row `project` field).
CHIEF_PROJECT_DIR_NAME = "fleet-config"


def live_title_names_chief(sess: Dict[str, Any]) -> bool:
    """Whether ``live_title`` — the OSC window-title session_host parses
    directly off the PTY's own raw output (``PtySession._read_loop``) —
    names this conversation "chief" (#628).

    This is the fastest of the three self-heal signals: it needs no hook and
    no transcript read, just Claude Code's own title escape sequence, which
    docs/board.md already documents as updating "sub-second inside an open
    terminal, ahead of the next state-file poll". Verified live against the
    real resumed chief session (7174c1d2…, the one this issue's own
    "Constraints worth knowing" section cites): at a moment when
    ``prompt_title`` and the hook-state ``shared_name`` had not yet
    identified it, ``live_title`` already read the conversation's
    self-declared title.

    Matched on the *last whitespace-separated token*, not equality — Claude
    prefixed its own title with an emoji (observed: a crown) in that live
    case, which carries no fixed spelling to match against, so pinning down
    only the trailing word avoids depending on which emoji (if any) it
    chooses.
    """
    title = str(sess.get("live_title") or "").strip()
    if not title:
        return False
    return title.split()[-1].strip().lower() == CHIEF_TITLE


def reconcile_chief_label(sess: Dict[str, Any], shared_name: Any) -> Dict[str, Any]:
    """Self-heal ``label`` for a chief PTY spawned outside ``ensure`` (#617).

    ``ensure_chief`` is spawn-if-absent, but it is not the only way a chief
    gets started. Two ways it slips past the label: Roberto opens a plain
    Coding-tab session in fleet-config and types ``/chief`` himself, or — the
    case actually observed live, verified against the real session
    (1c8e6dde…) rather than only synthetic tests — the session-host restarts,
    killing the PTY, and he re-attaches the *same underlying Claude Code
    conversation* via Resume. Either way the launcher never gets to pass
    ``label="chief"`` at spawn, so every consumer keying on it —
    :func:`find_chief`, ``chief_ops.py``'s worker-cap count and
    ``chief-sid`` lookup (both read straight off ``GET /api/board``, so this
    reconciliation is the only place any of them needs fixing), the Board's
    crown/tint — silently treats a live, working chief as not running.

    Three independent, narrow signals, any one sufficient on its own — not a
    stack of guesses, three genuinely different things a chief session does,
    each read from wherever Claude Code's own self-declared identity surfaces
    earliest:

    * ``live_title`` (#628, :func:`live_title_names_chief`): checked first
      here because it is available earliest — Claude Code re-emits its own
      established OSC title on a Resume, before any hook fires or the user
      types anything, closing the exact gap #628 was filed over (a resumed
      chief unidentifiable "until its first hook fires").
    * ``prompt_title`` (#266): the session-host's own capture of the first
      line ever *submitted* into this PTY. Exact for a freshly typed
      ``/chief`` — but a Resume never re-submits it (the conversation is
      already past that point), so this alone misses exactly the observed
      case.
    * ``shared_name`` (fleet-config#302): Claude Code's own self-derived name
      for the *conversation*, not the PTY — read from its live per-process
      registry via the hook state file, joined by the same agent-aware claim
      walk every other cross-tab title uses (:func:`board.attach_shared_names`).
      This persists across a Resume into a brand-new PTY, because it belongs
      to the conversation, not the process that's currently attached to it —
      but it needs a hook to have fired at least once, which is exactly the
      window ``live_title`` above closes.

    All three are scoped to a live PTY cwd'd in the fleet-config checkout — a
    directory name alone proves nothing (the dead ``name == "chief"``
    fallback below learned that the hard way: it reads the *launcher's*
    session name, which is the project name ("fleet-config") for a
    Resume-launched session, never "chief").

    Read-only: never mutates the session-host's own record, only the dict
    this process just fetched from it.
    """
    if sess.get("label") or sess.get("kind") != "pty":
        return sess
    if Path(str(sess.get("project_dir") or "")).name != CHIEF_PROJECT_DIR_NAME:
        return sess
    prompt_title = str(sess.get("prompt_title") or "").strip()
    shared_name_norm = str(shared_name or "").strip().lower()
    if (
        prompt_title != CHIEF_COMMAND
        and shared_name_norm != CHIEF_TITLE
        and not live_title_names_chief(sess)
    ):
        return sess
    logger.info(
        "👑 chief label self-healed for session %s (spawned outside ensure)",
        str(sess.get("session_id") or "")[:8],
    )
    return {**sess, "label": CHIEF_LABEL}


def reconcile_chief_labels(
    live: List[Dict[str, Any]], state_rows: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Apply :func:`reconcile_chief_label` across a live session list.

    Needs the hook state rows too: ``shared_name`` only exists after the
    state-row join. Read-only, like the per-session heal.
    """
    named = board.attach_shared_names(live, state_rows)
    shared_names = {
        str(item.get("session_id")): item.get("shared_name") for item in named
    }
    return [
        reconcile_chief_label(sess, shared_names.get(str(sess.get("session_id"))))
        for sess in live
    ]


def find_chief(live: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The alive chief PTY session, or {}. Matches the ``label`` tag with a
    ``name`` fallback so a legacy session-host that didn't echo ``label``
    still can't be double-spawned."""
    for sess in live:
        if not sess.get("alive") or sess.get("kind") != "pty":
            continue
        if sess.get("label") == CHIEF_LABEL or sess.get("name") == CHIEF_LABEL:
            return sess
    return {}
