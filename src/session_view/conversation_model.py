"""The model a stored conversation last ran on, read from the agent's own files (#1503).

Life OS's swipe-Resume promises that a resumed conversation comes back on its
own model, not on whatever the launch combo shows. The two harnesses differ,
and this was probed rather than assumed (recorded on #1503's PR):

- **Claude** — a bare ``claude --resume <id>`` keeps the conversation's model
  (started on Haiku with Opus as the configured default, resumed bare: Haiku),
  and ``--model`` on the resume overrides it. So the launcher sends no
  ``--model`` at all; the transcript is read only to *name* the model in the
  toast.
- **Codex** — a bare ``codex exec resume <id>`` falls back to the configured
  default model. So the launcher has to pass the conversation's model
  explicitly, and this module is where it comes from: Codex records the model
  of every turn in its rollout's ``turn_context`` lines.

Both readers follow ``life_os_transcript``'s rules: the id must be a canonical
UUID, the file name is built from it (never searched for by pattern text the
caller supplied), linked folders are never entered, and every failure answers
``""`` — "not known" — never a guess. Blocking filesystem work: callers run it
off the event loop.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.life_os.links import is_link
from src.life_os.transcript import SESSION_ID_RE, _project_folders, claude_projects_dir

logger = logging.getLogger(__name__)

# How much of a Claude transcript's end is read for the newest assistant
# record. The same window transcript_locate uses for the naming records, for
# the same reason: the record sits near the end, and a whole chief transcript
# runs to tens of MB.
_CLAUDE_TAIL_BYTES = 256 * 1024
_CLAUDE_MODEL_RE = re.compile(r'"model"\s*:\s*"(claude-[A-Za-z0-9.\-]+)"')
_CODEX_MODEL_RE = re.compile(r'"model"\s*:\s*"([A-Za-z0-9.\-_]+)"')


def codex_sessions_dir() -> Path:
    """``~/.codex/sessions``, resolved per call so a test's temporary home applies."""
    return Path.home() / ".codex" / "sessions"


def _claude_model(sid: str) -> str:
    for folder in _project_folders(claude_projects_dir()):
        path = folder / f"{sid}.jsonl"
        if is_link(path) or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - _CLAUDE_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError as exc:
            logger.warning("⚠️ conversation model: Claude transcript unreadable: %s", exc)
            return ""
        found = _CLAUDE_MODEL_RE.findall(tail)
        # "<synthetic>" records carry no real model; the regex already skips them.
        return found[-1] if found else ""
    return ""


def _codex_model(sid: str) -> str:
    root = codex_sessions_dir()
    try:
        paths = [p for p in root.rglob(f"rollout-*-{sid}.jsonl") if not is_link(p)]
    except OSError:
        return ""
    if len(paths) != 1:
        # None, or two rollouts claiming one id: neither is a fact to act on.
        return ""
    model = ""
    try:
        with paths[0].open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"turn_context"' not in line:
                    continue
                match = _CODEX_MODEL_RE.search(line)
                if match:
                    model = match.group(1)
    except OSError as exc:
        logger.warning("⚠️ conversation model: Codex rollout unreadable: %s", exc)
        return ""
    return model


def recorded_model(agent: str, sid: str) -> str:
    """The model id the conversation's newest turn ran on, or ``""`` when unknown.

    ``agent`` is the capture header's verified harness (``claude`` / ``codex``);
    anything else, or a non-UUID ``sid``, is ``""`` without touching the disk.
    """
    if not SESSION_ID_RE.fullmatch(sid or ""):
        return ""
    sid = sid.lower()
    if agent == "claude":
        return _claude_model(sid)
    if agent == "codex":
        return _codex_model(sid)
    return ""
