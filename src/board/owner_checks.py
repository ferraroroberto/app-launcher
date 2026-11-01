"""Owner checks on the Board: shipped work to validate, and steps owed before a start.

fleet-config's ``owner_check`` package owns the two labels (``needs-validation``,
``needs-owner``), the comment block and every read and write of them
(fleet-config ``docs/owner-check.md``). This module runs that package's command
line with fleet-config's own venv, never re-reading the labels itself:

* ``list --json`` fills an in-memory cache, read by the 5 s Board poll for free.
  The list costs two ``gh`` calls per fleet repo, so it runs only beside the
  Board's GitHub refresh, in a background thread with a cooldown.
* ``clear`` answers Validated and Done: it removes the label and posts the
  cleared block carrying the answer.

The launcher keeps two marks of its own, in ``webapp/owner-check-marks.json``:
``later`` (a snooze, until :data:`SNOOZE_HOURS` pass) and ``problem`` (the owner
reported one; the label stays until a fix ships and he validates). A marked item
stays listed but leaves the "needs you" count.

GitHub's label search trails a label change by minutes, so an item cleared here
is hidden for :data:`CLEARED_HIDE_SECONDS` even if the next list still returns it.

Degradation contract: a read that did not land is ``unknown``, never "none". A
never-filled cache says ``available: false``; a failed refresh keeps the older
items and sets ``error``; repos the list could not read come back in
``unknown``, so a count beside them is a lower bound.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src._json_io import atomic_write_json
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
MARKS_FILE = PROJECT_ROOT / "webapp" / "owner-check-marks.json"

VALIDATE_AFTER = "validate-after"
BEFORE_START = "before-start"

LATER = "later"
PROBLEM = "problem"
VALIDATED = "validated"
DONE = "done"

# What each kind can be answered with; the sheet offers the same set.
ANSWERS: Dict[str, Tuple[str, ...]] = {
    VALIDATE_AFTER: (VALIDATED, PROBLEM, LATER),
    BEFORE_START: (DONE, LATER),
}
# The text the cleared block's `answer` field carries.
_CLEAR_TEXT = {VALIDATED: "validated by owner", DONE: "done by owner"}

# Later snoozes an item for a day: back on the next morning's look.
SNOOZE_HOURS = 20
# A reported problem waits on a fix; a month without one drops the mark.
PROBLEM_MARK_DAYS = 30
# GitHub's issue-list index can lag a label removal by minutes.
CLEARED_HIDE_SECONDS = 15 * 60
# The fleet-wide list: two gh calls per repo, six in flight, a 60 s cap each.
LIST_TIMEOUT_SECONDS = 240
CLEAR_TIMEOUT_SECONDS = 90
# Refreshes no closer than this, however often the Board's GitHub refresh runs.
REFRESH_COOLDOWN_SECONDS = 60
PROBLEM_TEXT_MAX = 2000

_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "fetched_at": None,
    "items": [],
    "unknown": [],
    "error": None,
    "refreshing": False,
}
_last_refresh_started: Optional[float] = None
# ref -> monotonic time it was cleared from here.
_cleared: Dict[str, float] = {}


class OwnerCheckError(RuntimeError):
    """fleet-config's owner_check could not run, or a write did not land."""


def _now_iso(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


# ------------------------------------------------------------------ the CLI

def _cli_argv(fleet_config_dir: str) -> List[str]:
    """fleet-config's venv python and the owner_check CLI, or OwnerCheckError."""
    if not str(fleet_config_dir or "").strip():
        raise OwnerCheckError("the fleet-config folder is not set in Settings")
    root = Path(fleet_config_dir)
    script = root / "skills" / "_lib" / "owner_check" / "cli.py"
    python = root / ".venv" / "Scripts" / "python.exe"
    if not script.is_file():
        raise OwnerCheckError("this fleet-config checkout has no owner_check package")
    if not python.is_file():
        raise OwnerCheckError("fleet-config's .venv is missing")
    return [str(python), str(script)]


def _run_cli(fleet_config_dir: str, args: Sequence[str], timeout: float) -> Tuple[int, str]:
    """Run one owner_check command; ``(exit code, stdout)``. Tests replace this."""
    argv = _cli_argv(fleet_config_dir) + list(args)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=NO_WINDOW,
            env=_utf8_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OwnerCheckError(f"owner_check {args[0]} did not run: {exc}") from exc
    if proc.returncode != 0 and not proc.stdout.strip():
        detail = (proc.stderr or "").strip().splitlines()
        raise OwnerCheckError(
            f"owner_check {args[0]} exited {proc.returncode}: {detail[-1] if detail else 'no output'}"
        )
    return proc.returncode, proc.stdout


def _utf8_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return env


def _clean_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One list row as the Board shows it, or None for a row it can't place."""
    repo = str(raw.get("repo") or "").strip()
    number = raw.get("number")
    kind = raw.get("kind")
    if not repo or not isinstance(number, int) or kind not in ANSWERS:
        return None
    proof = raw.get("proof")
    return {
        "ref": f"{repo}#{number}",
        "repo": repo,
        "number": number,
        "title": str(raw.get("title") or ""),
        "url": str(raw.get("url") or ""),
        "state": str(raw.get("state") or ""),
        "kind": kind,
        # A label with no readable block: shown as "details unknown", never hidden.
        "block": bool(raw.get("block")),
        "reason": str(raw.get("reason") or ""),
        "try": str(raw.get("try") or ""),
        "proof": [str(p) for p in proof if str(p).strip()] if isinstance(proof, list) else [],
        "since": str(raw.get("since") or ""),
    }


def read_items(fleet_config_dir: str) -> Dict[str, Any]:
    """``{"items", "unknown"}`` from ``owner_check list --json``; OwnerCheckError
    when the list did not run or printed nothing it could parse."""
    _code, out = _run_cli(fleet_config_dir, ["list", "--json"], LIST_TIMEOUT_SECONDS)
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise OwnerCheckError(f"owner_check list printed unparseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise OwnerCheckError("owner_check list printed no object")
    items = [it for it in (_clean_item(r) for r in data.get("items") or [] if isinstance(r, dict)) if it]
    unknown = [
        {"repo": str(u.get("repo") or "?"), "reason": str(u.get("reason") or "")}
        for u in data.get("unknown") or [] if isinstance(u, dict)
    ]
    return {"items": items, "unknown": unknown}


# ------------------------------------------------------------------ the cache

def refresh(fleet_config_dir: str) -> Dict[str, Any]:
    """Read the list now and replace the cache; a failure keeps the older items."""
    fetched_at = _now_iso()
    try:
        result = read_items(fleet_config_dir)
    except OwnerCheckError as exc:
        logger.warning("⚠️ owner checks: list failed: %s", exc)
        with _lock:
            _cache.update(error=str(exc), refreshing=False)
        return snapshot()
    # The whole fleet unreadable is no read at all: keep what was there.
    if any(u["repo"] == "*" for u in result["unknown"]):
        reason = next(u["reason"] for u in result["unknown"] if u["repo"] == "*")
        logger.warning("⚠️ owner checks: fleet repo list unreadable: %s", reason)
        with _lock:
            _cache.update(error=reason, refreshing=False)
        return snapshot()
    with _lock:
        _cache.update(
            fetched_at=fetched_at, items=result["items"], unknown=result["unknown"],
            error=None, refreshing=False,
        )
    logger.info(
        "✅ owner checks: %d items, %d repos unreadable",
        len(result["items"]), len(result["unknown"]),
    )
    return snapshot()


def start_refresh(fleet_config_dir: str, *, now: Optional[float] = None) -> bool:
    """Refresh in a background thread unless one runs or ran within the cooldown."""
    global _last_refresh_started
    tick = time.monotonic() if now is None else now
    with _lock:
        if _cache["refreshing"]:
            return False
        if _last_refresh_started is not None and tick - _last_refresh_started < REFRESH_COOLDOWN_SECONDS:
            return False
        _cache["refreshing"] = True
        _last_refresh_started = tick
    threading.Thread(
        target=refresh, args=(fleet_config_dir,), name="owner-checks-refresh", daemon=True
    ).start()
    return True


def _recently_cleared(ref: str, tick: float) -> bool:
    at = _cleared.get(ref)
    return at is not None and tick - at < CLEARED_HIDE_SECONDS


def snapshot(*, now: Optional[datetime] = None, marks_path: Optional[Path] = None) -> Dict[str, Any]:
    """The Board's ``owner_checks`` section: the cached items with their marks."""
    moment = now or datetime.now(timezone.utc)
    tick = time.monotonic()
    marks = live_marks(read_marks(marks_path), moment)
    with _lock:
        cache = dict(_cache)
        items = [dict(it) for it in cache["items"] if not _recently_cleared(it["ref"], tick)]
    for item in items:
        mark = marks.get(item["ref"])
        item["mark"] = mark["mark"] if mark else None
        item["mark_at"] = mark["at"] if mark else None
        item["mark_until"] = mark.get("until") if mark else None
    return {
        "available": cache["fetched_at"] is not None,
        "fetched_at": cache["fetched_at"],
        "error": cache["error"],
        "refreshing": cache["refreshing"],
        "unknown": list(cache["unknown"]),
        "items": items,
    }


def reset_cache() -> None:
    """Test helper: the pristine never-fetched cache."""
    global _last_refresh_started
    with _lock:
        _cache.update(fetched_at=None, items=[], unknown=[], error=None, refreshing=False)
        _last_refresh_started = None
        _cleared.clear()


def _forget(ref: str) -> None:
    with _lock:
        _cleared[ref] = time.monotonic()
        _cache["items"] = [it for it in _cache["items"] if it["ref"] != ref]


# ------------------------------------------------------------------ the marks

def read_marks(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """``{ref: {"mark", "at", "until"?}}``; missing or corrupt reads as none."""
    target = Path(path or MARKS_FILE)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ owner-check marks unreadable: %s", exc.__class__.__name__)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(ref): mark for ref, mark in raw.items()
        if isinstance(mark, dict) and mark.get("mark") in (LATER, PROBLEM)
    }


def live_marks(marks: Dict[str, Dict[str, Any]], now: datetime) -> Dict[str, Dict[str, Any]]:
    """The marks still in force: an ended snooze and an old problem drop."""
    kept = {}
    for ref, mark in marks.items():
        if mark["mark"] == LATER:
            until = _parse_iso(mark.get("until"))
            if until is None or until <= now:
                continue
        else:
            at = _parse_iso(mark.get("at"))
            if at is None or now - at > timedelta(days=PROBLEM_MARK_DAYS):
                continue
        kept[ref] = mark
    return kept


def _write_mark(ref: str, mark: Optional[Dict[str, Any]], path: Optional[Path], now: datetime) -> None:
    target = Path(path or MARKS_FILE)
    with _lock:
        marks = live_marks(read_marks(target), now)
        if mark is None:
            marks.pop(ref, None)
        else:
            marks[ref] = mark
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, marks)


# ------------------------------------------------------------------ answers

def _split_ref(ref: str) -> Tuple[str, int]:
    repo, sep, num = str(ref).partition("#")
    if not sep or not repo or not num.isdigit():
        raise ValueError(f"expected <repo>#<N>, got {ref!r}")
    return repo, int(num)


def validate_answer(raw: Any) -> Dict[str, Any]:
    """One answer from the sheet, checked; ValueError says what is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("an answer must be an object")
    ref = str(raw.get("ref") or "").strip()
    repo, number = _split_ref(ref)
    kind = raw.get("kind")
    answer = raw.get("answer")
    if kind not in ANSWERS:
        raise ValueError(f"{ref}: unknown kind {kind!r}")
    if answer not in ANSWERS[kind]:
        raise ValueError(f"{ref}: {kind} cannot be answered {answer!r}")
    text = " ".join(str(raw.get("text") or "").split())[:PROBLEM_TEXT_MAX]
    if answer == PROBLEM and not text:
        raise ValueError(f"{ref}: a problem needs its description")
    return {"ref": ref, "repo": repo, "number": number, "kind": kind, "answer": answer, "text": text}


def problem_comment(text: str, today: str) -> str:
    """The comment a reported problem posts on the issue."""
    return f"**Owner validation found a problem** ({today})\n\n{text}\n"


def answer(
    fleet_config_dir: str,
    ans: Dict[str, Any],
    *,
    comment: Callable[[str, int, str], None],
    marks_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> None:
    """Apply one validated answer; OwnerCheckError when its write did not land.

    ``comment(repo, number, body)`` posts a plain comment (the launcher's ``gh``).
    """
    moment = now or datetime.now(timezone.utc)
    ref = ans["ref"]
    if ans["answer"] == LATER:
        until = moment + timedelta(hours=SNOOZE_HOURS)
        _write_mark(ref, {"mark": LATER, "at": _now_iso(moment), "until": _now_iso(until)}, marks_path, moment)
        logger.info("ℹ️ owner check %s snoozed until %s", ref, _now_iso(until))
        return
    if ans["answer"] == PROBLEM:
        comment(ans["repo"], ans["number"], problem_comment(ans["text"], moment.strftime("%Y-%m-%d")))
        _write_mark(ref, {"mark": PROBLEM, "at": _now_iso(moment)}, marks_path, moment)
        logger.info("ℹ️ owner check %s: problem reported", ref)
        return
    code, out = _run_cli(
        fleet_config_dir, ["clear", ref, "--answer", _CLEAR_TEXT[ans["answer"]]], CLEAR_TIMEOUT_SECONDS
    )
    if code != 0 or "CLEARED=unknown" in out:
        line = next((ln for ln in out.splitlines() if ln.startswith("CLEARED=")), out.strip()[:200])
        raise OwnerCheckError(f"{ref}: the clear did not land ({line})")
    _write_mark(ref, None, marks_path, moment)
    _forget(ref)
    logger.info("✅ owner check %s cleared: %s", ref, _CLEAR_TEXT[ans["answer"]])
