"""Board › Ideas: the inspiration scan's kept ideas and the ledger of what became of them.

fleet-config's ``inspiration-scan`` skill owns both files (``ideas.json`` and
``ideas-ledger.jsonl`` in its machine-local state root; the contract is that
repo's ``docs/inspiration-ideas.md``). The chief is the ledger's only writer, so
this module only reads: it serves the undecided ideas and the decided rows to
the Board, which sends the owner's decision to the chief's terminal.

A file that cannot be read is ``state: "unknown"`` with a reason, never an
empty list: a ledger line skipped here would offer a decided idea again.
No scan yet (no ideas file and no ledger, or no skill in the fleet-config
checkout) is ``state: "missing"``, which the Board hides. Idea text is
the owner's reading, so nothing but counts and reasons is ever logged.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src._log_once import log_once
from src.inspiration import sources

logger = logging.getLogger(__name__)

IDEAS_FILE = "ideas.json"
LEDGER_FILE = "ideas-ledger.jsonl"
STATES = ("applied", "filtered")
# The Board lists these at most; the ledger is append-only and only grows.
MAX_IDEAS = 200
MAX_LEDGER = 200
_TEXT_FIELDS = ("id", "title", "link", "author", "published", "excerpt", "digest", "model",
                "why_fits", "experiment", "suggested_repo", "prompt")
_ROW_FIELDS = ("id", "state", "at", "issue", "note", "title", "link", "author", "published", "digest", "model")

# The chief files in `<owner>/<repo>`; a ref of any other shape gets no link.
_REF = re.compile(r"^([A-Za-z0-9_.-]+)#([0-9]+)$")

_logged_failures: set[str] = set()
_FAILURE_LOG_CAP = 64


class _Unreadable(Exception):
    """A file exists but cannot be served; the message is the reason the Board shows."""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _score(value: Any) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _source(value: Any) -> Dict[str, str]:
    value = value if isinstance(value, dict) else {}
    return {"name": _text(value.get("name")), "kind": _text(value.get("kind"))}


def _clean_idea(raw: Dict[str, Any]) -> Dict[str, Any]:
    idea: Dict[str, Any] = {key: _text(raw.get(key)) for key in _TEXT_FIELDS}
    idea["source"] = _source(raw.get("source"))
    idea["score"] = _score(raw.get("score"))
    return idea


def _issue_url(ref: str, owner: str) -> str:
    match = _REF.match(ref)
    return f"https://github.com/{owner}/{match.group(1)}/issues/{match.group(2)}" if match and owner else ""


def _clean_row(raw: Dict[str, Any], owner: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {key: _text(raw.get(key)) for key in _ROW_FIELDS}
    row["source"] = _source(raw.get("source"))
    row["score"] = _score(raw.get("score"))
    row["issue_url"] = _issue_url(row["issue"], owner)
    return row


def _read_ideas(root: Path) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """``(digest, ideas)``; ``(None, [])`` when the file does not exist."""
    path = root / IDEAS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        raise _Unreadable(f"{IDEAS_FILE} cannot be read: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise _Unreadable(f"{IDEAS_FILE} is not valid JSON.") from exc
    rows = data.get("ideas") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _Unreadable(f"{IDEAS_FILE} has no ideas list.")
    ideas = [_clean_idea(r) for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]]
    return _text(data.get("digest")), ideas


def _read_ledger(root: Path, owner: str) -> Optional[List[Dict[str, Any]]]:
    """Every ledger row in file order; ``None`` when the file does not exist."""
    path = root / LEDGER_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unreadable(f"{LEDGER_FILE} cannot be read: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise _Unreadable(f"{LEDGER_FILE} is not UTF-8 text.") from exc
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise _Unreadable(f"{LEDGER_FILE} line {number} is not JSON.") from exc
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row.get("state") not in STATES:
            raise _Unreadable(f"{LEDGER_FILE} line {number} is not a ledger row.")
        rows.append(_clean_row(row, owner))
    return rows


def _empty(state: str, reason: str) -> Dict[str, Any]:
    return {"state": state, "reason": reason, "digest": "", "ideas": [], "ledger": [], "sources": None}


def _skill_absent(fleet_config_dir: str) -> bool:
    """No folder set, or a checkout without the skill: no scan can have run, so there is nothing to show."""
    folder = str(fleet_config_dir or "").strip()
    return not folder or not (Path(folder) / sources.SKILL_SUBPATH / "sources.py").is_file()


def read_board_ideas(fleet_config_dir: str, github_owner: str = "") -> Dict[str, Any]:
    """What the Board's Ideas card draws. Blocking: wrap in a thread.

    ``ideas`` are the undecided ones, best score first; ``ledger`` is the decided
    rows, newest first. ``sources`` is the latest digest's status line.
    """
    root, reason = sources.state_root_for(fleet_config_dir)
    if root is None:
        return _empty("missing" if _skill_absent(fleet_config_dir) else "unknown", reason)
    try:
        digest, ideas = _read_ideas(root)
        ledger = _read_ledger(root, github_owner)
    except _Unreadable as exc:
        log_once(_logged_failures, str(exc), _FAILURE_LOG_CAP, logger.warning,
                 "⚠️ board ideas: %s", exc)
        return _empty("unknown", str(exc))
    if digest is None and ledger is None:
        return _empty("missing", "")
    decided = {row["id"] for row in ledger or []}
    pending = [i for i in ideas if i["id"] not in decided]
    pending.sort(key=lambda i: -(i["score"] if i["score"] is not None else -1))
    return {
        "state": "ok", "reason": "", "digest": digest or "",
        "ideas": pending[:MAX_IDEAS],
        "ledger": list(reversed(ledger or []))[:MAX_LEDGER],
        "sources": sources.digest_status(root),
    }
