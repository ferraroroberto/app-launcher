"""Agent-aware conversation previews for the Board drawer (issue #457).

The hook row's Claude JSONL remains the best source when it exists because it
is structured chat data.  Launcher-owned PTYs also have an exact-id capture,
however, and that is the common fallback for remote-control Claude sessions
whose declared JSONL is absent and for agents such as Codex that publish no
hook transcript at all.

The capture is terminal output, not prose.  A bounded tail is replayed through
``pyte`` and reply blocks are selected by the same leading-bullet colour
contract as the browser's read-aloud extractor.  This keeps ANSI/full-screen
repaint bytes out of the API response and avoids any cwd-based guessing.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pyte

from src.board_transcript import _read_tail_bytes, _tail_lines, last_exchange

_CAPTURE_TAIL_BYTES = 512 * 1024
# The launcher input log (``webapp/sessions/<sid>.log``) is appended one
# ``[input]`` line per chunk for a session's whole life, with no rotation, and
# the chief drawer re-polls it every 5s — so only its tail is read (#881), like
# every other reader here. Sized by measurement against the 12 largest logs
# on this box: some sessions log 100+ KB of non-submitting input (keys,
# terminal escape traffic) after their last Enter, so 64 KB changed the shown
# prompt on 3 of 12 while 256 KB matched a full read on all 12 — the same
# window as ``board_transcript._EXCHANGE_TAIL_BYTES``. A submission older than
# the window falls back to the session's prompt title.
_INPUT_TAIL_BYTES = 256 * 1024
_CAPTURE_HISTORY_LINES = 2500
_CODEX_TAIL_BYTES = 4 * 1024 * 1024
_CODEX_START_SLOP_SECONDS = 120
_ASSISTANT_TEXT_CAP = 6000
_USER_TEXT_CAP = 1500

_BULLETS = frozenset({"●", "⏺", "•", "◉", "○"})
_SATURATED_NAMED = frozenset({
    "black", "red", "green", "brown", "blue", "magenta", "cyan",
    "brightblack", "brightred", "brightgreen", "brightblue",
    "brightmagenta", "brightcyan",
})
_LEAD_BULLET_RE = re.compile(r"^[●⏺•◉○]\s+")
_TOOL_CALL_RE = re.compile(r"^[●⏺•◉○]\s+[A-Z][A-Za-z0-9_-]*\(")
_RULE_RUN_RE = re.compile(r"[─━═┄┅┈┉╌╍]{6,}")
_RULE_CHARS_RE = re.compile(r"[─-▟│┄┅┈┉╌╍]")
_TIMING_RE = re.compile(
    r"^\s*[*✶✻✽✢✱·•∗⁘]?\s*[A-Z][a-z]+ for \d+\s*[smhd]\b"
)
_SPINNER_RE = re.compile(
    r"^\s*[*✶✻✽✢✱·•∗⁘]?\s*[A-Z][a-z]+(?:…|\.\.\.)\s*\("
)
_TIP_RE = re.compile(r"^\s*[⎿└╰⤷↳]\s*Tip\b", re.IGNORECASE)
_INPUT_RE = re.compile(r"\[input\]\s+(.*)$")
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CODEX_FILE_RE = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-.*\.jsonl$"
)
_CODEX_CWD_RE = re.compile(rb'"cwd"\s*:\s*"((?:\\.|[^"\\])*)"')
_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
# A Pi session id is a UUID; anything else is refused before it reaches
# `glob`, where `*`/`?`/`[` would be pattern syntax rather than a literal.
_PI_SID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
_COPILOT_STATE_DIR = Path.home() / ".copilot" / "session-state"
# Copilot's own session folder is created at launch, a little *after* the
# launcher spawned it: measured at 3.71 s and 3.72 s on two launches here.
# The window is wide enough to absorb a loaded box (16x the measured delay)
# and narrow enough that two launches in one folder rarely both fall in it —
# and when they do, `find_copilot_transcript` refuses rather than guesses.
_COPILOT_START_SLOP_SECONDS = 60
# Copilot stamps `created_at` from its own clock, so allow it to read a hair
# before the launcher's `started_at` without disqualifying the folder.
_COPILOT_CLOCK_SKEW_SECONDS = 5
# `workspace.yaml` is ~500 bytes. Read bounded anyway, like every reader here.
_COPILOT_YAML_CAP_BYTES = 64 * 1024
# `\r` is matched explicitly on both sides: `re.M`'s `$` stops before a `\n`
# but leaves a CRLF file's `\r` in the way, which would strand it inside the
# captured `cwd` and block the `created_at` match outright. The real files
# here are LF, but nothing about the format promises that.
_COPILOT_CWD_RE = re.compile(r"^cwd:[ \t]*(.+?)[ \t\r]*$", re.M)
_COPILOT_CREATED_RE = re.compile(r"^created_at:[ \t]*(\S+?)[ \t\r]*$", re.M)
_AGY_ROOT = Path.home() / ".gemini" / "antigravity-cli"
# Antigravity names a conversation directory by UUID; the strict shape keeps
# a mangled cache key from being joined onto a path.
_AGY_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
# `cache/last_conversations.json` is a flat workspace→uuid map, a few KB on a
# box with 100 workspaces. Read bounded anyway, like every other reader here.
_AGY_CACHE_CAP_BYTES = 1024 * 1024
# How far before a session's own `started_at` its conversation's last write
# may sit and still count as "written by this session" — the launcher stamps
# `started_at` when it spawns the PTY, the harness writes a moment later, and
# clock granularity between the two is not worth a false negative.
_AGY_MTIME_SLOP_SECONDS = 60
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
# Claude Code names a project folder after its cwd with every non-alphanumeric
# character replaced by `-`. Measured across the 107 folders on this box whose
# own JSONL records a `cwd`: 103 match that transform exactly and 4 older ones
# are additionally lowercased, so the folder is matched case-insensitively
# rather than by building one name.
_CLAUDE_SLUG_RE = re.compile(r"[^A-Za-z0-9]")
# Same meaning as `_AGY_MTIME_SLOP_SECONDS`, for the same reason.
_CLAUDE_MTIME_SLOP_SECONDS = 60


def unavailable(reason: str) -> Dict[str, Any]:
    """Canonical unavailable response with a machine-readable reason."""
    return {
        "available": False,
        "source": None,
        "reason": reason,
        "user": None,
        "assistant": None,
    }


def resolve_exchange(
    session: Dict[str, Any],
    native_path: Any,
    launcher_capture_path: Path,
    launcher_input_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve one live session's exchange through the source hierarchy."""
    native = last_exchange(native_path)
    if native.get("available"):
        return {**native, "source": "native", "reason": None}

    if str(session.get("agent") or "claude").lower() == "codex":
        codex_path = _find_codex_transcript(session)
        codex = codex_last_exchange(codex_path)
        if codex.get("available"):
            return codex

    fallback = launcher_last_exchange(
        launcher_capture_path,
        launcher_input_path=launcher_input_path,
        prompt_fallback=str(session.get("prompt_title") or "").strip(),
        rows=int(session.get("rows") or 42),
        cols=int(session.get("cols") or 120),
    )
    if fallback.get("available"):
        return fallback

    native_declared = bool(native_path)
    capture_exists = _nonempty_file(launcher_capture_path)
    if capture_exists:
        reason = str(fallback.get("reason") or "capture_unparseable")
    elif native_declared:
        reason = "native_unavailable"
    else:
        reason = "no_exchange"
    return unavailable(reason)


def codex_last_exchange(path: Optional[Path]) -> Dict[str, Any]:
    """Read the newest Codex user/assistant messages from bounded JSONL."""
    if path is None:
        return unavailable("native_unavailable")
    raw = _read_tail(path, _CODEX_TAIL_BYTES)
    if not raw:
        return unavailable("native_unavailable")
    records: List[Tuple[str, str, Any]] = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = str(payload.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        wanted = "input_text" if role == "user" else "output_text"
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        text = "\n\n".join(
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == wanted
            and str(item.get("text") or "").strip()
        )
        if text:
            records.append((role, text, obj.get("timestamp")))
    assistant_index = next(
        (index for index in range(len(records) - 1, -1, -1)
         if records[index][0] == "assistant"),
        None,
    )
    if assistant_index is None:
        return unavailable("no_exchange")
    user_record = next(
        (records[index] for index in range(assistant_index - 1, -1, -1)
         if records[index][0] == "user"),
        None,
    )
    assistant = records[assistant_index]
    return {
        "available": True,
        "source": "codex",
        "reason": None,
        "user": (
            {
                "text": user_record[1][-_USER_TEXT_CAP:],
                "timestamp": user_record[2],
            }
            if user_record else None
        ),
        "assistant": {
            "text": assistant[1][-_ASSISTANT_TEXT_CAP:],
            "timestamp": assistant[2],
        },
    }


def _find_codex_transcript(session: Dict[str, Any]) -> Optional[Path]:
    """Safely correlate a Codex rollout by cwd + launch timestamp.

    Codex does not persist ``APP_LAUNCHER_SESSION_ID`` in its JSONL.  Filename
    start time plus the transcript's cwd is therefore used only when there is
    one unambiguous candidate in a narrow launch window.  Ambiguity degrades
    to the exact-id launcher capture rather than risking cross-session text.
    """
    started_raw = session.get("started_at")
    try:
        if isinstance(started_raw, (int, float)):
            started = datetime.fromtimestamp(float(started_raw)).astimezone()
        else:
            started = datetime.fromisoformat(
                str(started_raw).replace("Z", "+00:00")
            ).astimezone()
    except (TypeError, ValueError, OSError):
        return None
    session_cwd = _normalize_dir(session.get("project_dir"))
    if not session_cwd:
        return None

    day_dir = _CODEX_SESSIONS_DIR / started.strftime("%Y/%m/%d")
    candidates: List[Tuple[float, Path]] = []
    try:
        paths = list(day_dir.glob("rollout-*.jsonl"))
    except OSError:
        return None
    for path in paths:
        match = _CODEX_FILE_RE.match(path.name)
        if not match:
            continue
        try:
            file_started = datetime.strptime(
                match.group(1), "%Y-%m-%dT%H-%M-%S"
            ).replace(tzinfo=started.tzinfo)
        except ValueError:
            continue
        delta = abs((file_started - started).total_seconds())
        if delta > _CODEX_START_SLOP_SECONDS:
            continue
        if _codex_cwd(path) != session_cwd:
            continue
        candidates.append((delta, path))
    candidates.sort(key=lambda item: item[0])
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 1.0:
        return None
    return candidates[0][1]


def find_pi_transcript(state_sid: str) -> Optional[Path]:
    """The Pi history file for a state row keyed ``state_sid`` (#1013).

    Pi's fleet ``session_state`` extension keys its row by **Pi's own
    session uuid** and leaves ``transcript_path`` null, while Pi names the
    file ``<start ISO ts>_<that same uuid>.jsonl`` under a per-cwd folder.
    The uuid is therefore an exact key, so — unlike
    :func:`_find_codex_transcript`, which has only cwd + launch time to go
    on and must reason about slop windows — this needs no heuristic and no
    reconstruction of Pi's cwd-to-folder name mangling: one glob across the
    session folders answers it.

    Fails the same way regardless: anything but exactly one match returns
    ``None``, so the caller says "no transcript" rather than risking a
    neighbouring session's text.
    """
    if not _PI_SID_RE.match(str(state_sid or "")):
        return None
    try:
        matches = list(_PI_SESSIONS_DIR.glob(f"*/*_{state_sid}.jsonl"))
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def _started_epoch(session: Dict[str, Any]) -> Optional[float]:
    """A live session's ``started_at`` as epoch seconds, or None.

    The session-host reports it as a float, but a row read back from JSON
    state can carry an ISO string, so both are accepted.
    """
    raw = session.get("started_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def find_antigravity_transcript(
    session: Dict[str, Any], live: Iterable[Dict[str, Any]]
) -> Optional[Path]:
    """The Antigravity conversation log for a live session (#1014).

    Antigravity writes nothing that names the launcher session: its
    conversation store is keyed by its own UUID, and `history.jsonl` — the
    one file appended per typed prompt — **omits `conversationId` on a
    conversation's first row** (measured on a session launched through the
    launcher's own API: the first prompt's row carries only `display`,
    `timestamp` and `workspace`). So a one-prompt session cannot be
    correlated through it at all.

    What *is* written the moment the first prompt lands, and updated while
    the session is still running, is ``cache/last_conversations.json`` —
    a flat ``workspace path → newest conversation uuid`` map. That is the
    correlation: the session's own ``project_dir``, looked up there.

    It answers "the newest conversation in this folder", not "this
    session's conversation", so two guards make the difference fail safe:

    * **One live session per folder.** If the launcher is hosting another
      live Antigravity session with the same ``project_dir``, the map
      cannot say which of them the uuid belongs to — refuse, rather than
      show one session's text under the other's name.
    * **Written since this session started.** The conversation log's mtime
      must be at or after ``started_at`` (less
      :data:`_AGY_MTIME_SLOP_SECONDS`). A session whose user has not typed
      yet leaves the map pointing at whatever ran in that folder before,
      and that file has not been touched since — so it is refused, which is
      also the right answer (there is no transcript yet).

    Prefers ``transcript_full.jsonl`` over ``transcript.jsonl``: the flat
    file truncates long ``content``/``thinking`` (naming the casualties in
    ``truncated_fields``) and JSON-encodes each tool argument a second
    time, so it cannot serve ``entry_full_text``'s uncapped promise. 55 of
    265 conversations on this box have only the flat file, for no reason
    established by the probe, so it stays a fallback rather than an error.
    """
    project_dir = _normalize_dir(session.get("project_dir"))
    started = _started_epoch(session)
    if not project_dir or started is None:
        return None
    sid = str(session.get("session_id") or "")
    for other in live or ():
        if (
            str(other.get("session_id") or "") != sid
            and str(other.get("agent") or "").lower() == "antigravity"
            and other.get("alive")
            and _normalize_dir(other.get("project_dir")) == project_dir
        ):
            return None

    try:
        with (_AGY_ROOT / "cache" / "last_conversations.json").open("rb") as fh:
            raw = fh.read(_AGY_CACHE_CAP_BYTES)
        mapping = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(mapping, dict):
        return None
    uuid = next(
        (
            str(value)
            for key, value in mapping.items()
            if _normalize_dir(key) == project_dir
        ),
        "",
    )
    if not _AGY_UUID_RE.match(uuid):
        return None

    logs = _AGY_ROOT / "brain" / uuid / ".system_generated" / "logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        path = logs / name
        try:
            written = path.stat().st_mtime
        except OSError:
            continue
        return path if written >= started - _AGY_MTIME_SLOP_SECONDS else None
    return None



def _copilot_workspace(path: Path) -> Tuple[str, Optional[float]]:
    """``(cwd, created_at epoch)`` from one Copilot ``workspace.yaml``.

    Two flat scalars off the top of a ~500-byte file, read with a regex
    rather than a YAML parser: the repo has no YAML dependency, and the two
    keys wanted are unquoted one-line scalars. A file that is missing,
    unreadable or malformed answers ``("", None)``, which no session matches.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(_COPILOT_YAML_CAP_BYTES)
    except OSError:
        return "", None
    text = raw.decode("utf-8", errors="replace")
    cwd_match = _COPILOT_CWD_RE.search(text)
    created_match = _COPILOT_CREATED_RE.search(text)
    if not cwd_match or not created_match:
        return "", None
    try:
        created = datetime.fromisoformat(
            created_match.group(1).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OSError):
        return "", None
    return _normalize_dir(cwd_match.group(1)), created


def find_copilot_transcript(session: Dict[str, Any]) -> Optional[Path]:
    """The Copilot CLI event log for a live session (#1015).

    Copilot writes the launcher nothing: its hooks don't fire in the
    interactive TUI, so there is no sessions-state row, and a grep of a
    launcher-spawned session's whole log for the launcher's own session id
    finds zero hits. What it *does* write, at launch and before any prompt,
    is a per-session folder holding ``workspace.yaml`` with the two fields
    this correlates on — ``cwd`` and ``created_at``.

    So this is the launch-window match ``_find_codex_transcript`` makes,
    against the harness's own sidecar rather than a filename: a folder
    qualifies when its ``cwd`` is this session's ``project_dir`` and its
    ``created_at`` falls within :data:`_COPILOT_START_SLOP_SECONDS` after
    the session's ``started_at`` (measured: 3.71 s and 3.72 s).

    It fails safe the way every flavour here does — **anything other than
    exactly one qualifying folder returns ``None``**, so the caller answers
    "no transcript" rather than risking a neighbouring session's text. Two
    sessions launched in one folder inside the window are ambiguous by
    construction and both refuse; being *outside* each other's window is
    what makes the ordinary case unambiguous, not a tie-break.

    A qualifying folder whose ``events.jsonl`` does not exist yet also
    answers ``None`` — Copilot creates that file at the first prompt, so a
    session nobody has typed into genuinely has no transcript. Older
    sessions on this box keep a ``session.db`` instead (28 of 71) and are
    refused by the same check.

    Cost: one bounded read of every session folder's sidecar — 71 files of
    ~500 bytes here. Chat mode has no periodic refresh (#982), so this runs
    on opening the pane and on Reload, not on a timer.
    """
    project_dir = _normalize_dir(session.get("project_dir"))
    started = _started_epoch(session)
    if not project_dir or started is None:
        return None
    try:
        workspaces = list(_COPILOT_STATE_DIR.glob("*/workspace.yaml"))
    except OSError:
        return None

    matches: List[Path] = []
    for workspace in workspaces:
        cwd, created = _copilot_workspace(workspace)
        if created is None or cwd != project_dir:
            continue
        delta = created - started
        if -_COPILOT_CLOCK_SKEW_SECONDS <= delta <= _COPILOT_START_SLOP_SECONDS:
            matches.append(workspace.parent / "events.jsonl")
        if len(matches) > 1:
            return None
    if len(matches) != 1 or not matches[0].is_file():
        return None
    return matches[0]


def find_claude_transcript(
    session: Dict[str, Any], live: Iterable[Dict[str, Any]]
) -> Optional[Path]:
    """The Claude Code conversation log for a live session, from the
    filesystem alone (#1023) — the fallback for when no state row names one.

    Claude's history normally arrives the direct way, on the hook state row's
    ``transcript_path``. That row is written by ``fleet-config``'s
    ``session_state`` hook, and its ``SessionEnd`` event **deletes** it, then
    tombstones the conversation for 24 h so only a later ``UserPromptSubmit``
    can bring it back. Claude Code fires ``SessionEnd`` on ``/resume`` and
    ``/clear`` as well as on exit — the process lives on under a different
    conversation — so between a ``/resume`` and the user's next prompt a
    perfectly live session has no row at all, and Chat mode said "no
    transcript" over a conversation sitting readable on disk. A session whose
    hook never ran, and one whose row aged out of the 24 h prune, land in the
    same place.

    Nothing in the JSONL names the launcher (a conversation opens with
    ``mode`` / ``permission-mode`` rows; the rest carry ``sessionId`` and
    ``cwd``), and Claude Code's own live per-process registry
    (``~/.claude/sessions/<pid>.json``), which does map a pid to a
    conversation, goes stale on ``/resume`` — measured here still naming the
    pre-resume conversation eleven hours and two conversations later. So the
    correlation is the folder: Claude files a conversation under
    ``~/.claude/projects/<cwd with every non-alphanumeric replaced by ->``,
    and the newest ``*.jsonl`` in the session's own folder is its own — the
    sixth correlation mechanism in this reader, and again not portable from
    any of the five before it.

    "Newest in this folder" is a weaker claim than an id, so two guards make
    the difference fail safe, the same shape as
    :func:`find_antigravity_transcript`:

    * **One live session per folder.** If the launcher hosts another alive
      Claude session with the same ``project_dir``, nothing here can say
      which of them the newest file belongs to — refuse, rather than show
      one session's conversation under the other's name (#537).
    * **Written since this session started.** The file's mtime must be at or
      after ``started_at`` (less :data:`_CLAUDE_MTIME_SLOP_SECONDS`). A
      session that has not written yet would otherwise inherit whatever ran
      in that directory before it, and "no transcript yet" is the truthful
      answer there.

    Both refusals answer ``None``, which the caller reports as
    ``no_transcript`` — never an ``available: true`` over a file that may not
    be this session's.
    """
    project_dir = str(session.get("project_dir") or "")
    started = _started_epoch(session)
    if not project_dir or started is None:
        return None
    normalized = _normalize_dir(project_dir)
    sid = str(session.get("session_id") or "")
    for other in live or ():
        if (
            str(other.get("session_id") or "") != sid
            and str(other.get("agent") or "claude").strip().lower() == "claude"
            and other.get("alive")
            and _normalize_dir(other.get("project_dir")) == normalized
        ):
            return None

    # Built from the *normalized* directory so the session's own spelling —
    # separator flavour, trailing slash, drive-letter case — cannot change
    # the folder it looks for.
    slug = _CLAUDE_SLUG_RE.sub("-", normalized)
    newest: Optional[Path] = None
    newest_at = 0.0
    try:
        folders = [
            child
            for child in _CLAUDE_PROJECTS_DIR.iterdir()
            if child.name.lower() == slug and child.is_dir()
        ]
    except OSError:
        return None
    for folder in folders:
        try:
            candidates = list(folder.glob("*.jsonl"))
        except OSError:
            continue
        for path in candidates:
            try:
                written = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or written > newest_at:
                newest, newest_at = path, written
    if newest is None:
        return None
    return newest if newest_at >= started - _CLAUDE_MTIME_SLOP_SECONDS else None


def _codex_cwd(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            prefix = fh.read(64 * 1024)
    except OSError:
        return ""
    match = _CODEX_CWD_RE.search(prefix)
    if not match:
        return ""
    try:
        value = json.loads('"' + match.group(1).decode("utf-8") + '"')
    except (UnicodeDecodeError, ValueError):
        return ""
    return _normalize_dir(value)


def _normalize_dir(raw: Any) -> str:
    return str(raw or "").replace("\\", "/").rstrip("/").lower()


def launcher_last_exchange(
    capture_path: Path,
    *,
    launcher_input_path: Optional[Path] = None,
    prompt_fallback: str = "",
    rows: int = 42,
    cols: int = 120,
) -> Dict[str, Any]:
    """Extract the latest reply from an exact-id PTY capture tail."""
    raw = _read_tail(capture_path, _CAPTURE_TAIL_BYTES)
    if not raw:
        return unavailable("no_exchange")

    parsed_rows = _terminal_rows(raw, rows=max(2, rows), cols=max(20, cols))
    blocks = _reply_blocks(parsed_rows)
    prompt = _last_submitted_input(launcher_input_path) or prompt_fallback
    if not blocks:
        return unavailable("capture_unparseable" if prompt else "no_exchange")

    return {
        "available": True,
        "source": "launcher",
        "reason": None,
        "user": (
            {"text": prompt[-_USER_TEXT_CAP:], "timestamp": None}
            if prompt else None
        ),
        "assistant": {
            "text": blocks[-1][-_ASSISTANT_TEXT_CAP:],
            "timestamp": None,
        },
    }


def _read_tail(path: Path, n_bytes: int) -> str:
    return _read_tail_bytes(path, n_bytes).decode("utf-8", errors="replace")


def _nonempty_file(path: Path) -> bool:
    try:
        return Path(path).stat().st_size > 0
    except OSError:
        return False


def _terminal_rows(raw: str, *, rows: int, cols: int) -> List[Tuple[str, str]]:
    screen = pyte.HistoryScreen(cols, rows, history=_CAPTURE_HISTORY_LINES)
    pyte.Stream(screen).feed(raw)
    all_rows: Iterable[Any] = list(screen.history.top) + [
        screen.buffer[y] for y in range(screen.lines)
    ]
    return [(_plain_row(row, cols), _row_marker(row, cols)) for row in all_rows]


def _plain_row(row: Any, cols: int) -> str:
    return "".join((row[x].data or " ") for x in range(cols)).rstrip()


def _row_marker(row: Any, cols: int) -> str:
    for x in range(cols):
        cell = row[x]
        char = cell.data or " "
        if not char.strip():
            continue
        if char not in _BULLETS:
            return "none"
        # Claude dims an in-flight tool bullet to neutral grey. Colour spread
        # then looks assistant-like, but the stable ``ToolName(...)`` shape is
        # still definitive and prevents a running Bash/Read block replacing
        # the last completed prose reply in the drawer.
        if _TOOL_CALL_RE.search(_plain_row(row, cols).lstrip()):
            return "tool"
        return "tool" if _is_tool_color(str(cell.fg or "default")) else "assistant"
    return "none"


def _is_tool_color(color: str) -> bool:
    color = color.lower()
    if color in ("default", "white", "brightwhite"):
        return False
    if color in _SATURATED_NAMED:
        return True
    if re.fullmatch(r"[0-9a-f]{6}", color):
        channels = [int(color[i:i + 2], 16) for i in (0, 2, 4)]
        return max(channels) - min(channels) > 60
    return False


def _reply_blocks(rows: List[Tuple[str, str]]) -> List[str]:
    rows = _drop_trailing_composer(rows)
    blocks: List[str] = []
    current: Optional[List[str]] = None
    for text, marker in rows:
        if marker == "assistant":
            _flush_block(current, blocks)
            current = [text]
        elif marker == "tool":
            _flush_block(current, blocks)
            current = None
        elif current is not None:
            current.append(text)
    _flush_block(current, blocks)
    return blocks


def _flush_block(current: Optional[List[str]], blocks: List[str]) -> None:
    if not current:
        return
    prose: List[str] = []
    for line in current:
        stripped = line.strip()
        if (
            _TIMING_RE.search(stripped)
            or _SPINNER_RE.search(stripped)
            or stripped.lower().startswith("recap")
            or _TIP_RE.search(line)
        ):
            break
        prose.append(line)
    text = re.sub(r"\s+", " ", " ".join(prose)).strip()
    text = _LEAD_BULLET_RE.sub("", text)
    if text:
        blocks.append(text)


def _drop_trailing_composer(rows: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    last_rule = -1
    for index in range(len(rows) - 1, -1, -1):
        if _is_rule_line(rows[index][0]):
            last_rule = index
            break
    if last_rule < 0:
        return rows
    top = last_rule
    gap = 0
    for index in range(last_rule - 1, -1, -1):
        if gap > 4:
            break
        if _is_rule_line(rows[index][0]):
            top = index
            gap = 0
        else:
            gap += 1
    return rows[:top]


def _is_rule_line(line: str) -> bool:
    if _RULE_RUN_RE.search(line):
        return True
    nonspace = re.sub(r"\s", "", line)
    if len(nonspace) < 8:
        return False
    return len(_RULE_CHARS_RE.findall(nonspace)) / len(nonspace) >= 0.8


def _last_submitted_input(path: Optional[Path]) -> str:
    if path is None:
        return ""
    lines, truncated = _tail_lines(path, _INPUT_TAIL_BYTES)
    if truncated and lines:
        lines = lines[1:]  # likely torn by the seek
    buffer = ""
    submitted: List[str] = []
    for line in lines:
        match = _INPUT_RE.search(line)
        if not match:
            continue
        try:
            chunk = ast.literal_eval(match.group(1))
        except (SyntaxError, ValueError):
            continue
        if not isinstance(chunk, str):
            continue
        chunk = chunk.replace("\x1b[200~", "").replace("\x1b[201~", "")
        chunk = _CSI_RE.sub("", chunk)
        for char in chunk:
            if char in ("\x7f", "\b"):
                buffer = buffer[:-1]
            elif char in ("\r", "\n"):
                text = buffer.strip()
                if text:
                    submitted.append(text)
                buffer = ""
            elif char == "\t" or ord(char) >= 32:
                buffer += char
    return submitted[-1] if submitted else ""
