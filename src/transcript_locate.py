"""Filesystem correlation of a live session to its harness transcript.

Split out of ``board_exchange.py`` (#1309): that module resolves *what to
show* for the Board drawer's conversation preview, while this one answers a
narrower, reusable question — *which file on disk belongs to this live
session* — for every harness that writes the launcher no exact id at all
(Codex, Pi, Antigravity, Copilot) plus Claude's own filesystem fallback
(#1023) and its ``--resume <id>`` correlation (#1155). ``board_exchange``
and ``app/webapp/routers/session_transcript.py`` both call into this module
rather than duplicating the correlation logic; neither owns it.

Every finder here fails the same way: anything short of exactly one
unambiguous, safely-correlated candidate returns ``None`` rather than risk
showing a neighbouring session's text.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src._log_once import log_once
from src.board_transcript import _read_tail_bytes, strip_status_glyph

logger = logging.getLogger(__name__)

_CODEX_TAIL_BYTES = 4 * 1024 * 1024
# How far back to look for the naming records a scanned Claude conversation
# declares about itself (#1034), read with the same bounded-tail discipline as
# every other reader here. Claude Code re-emits `ai-title`/`custom-title`
# periodically rather than once, so the newest one sits near the end of the
# file — measured across the 8 newest conversations in three project folders
# on this box, the last naming record sat at most 22.7 KB from EOF, including
# in a 67.8 MB chief conversation. 256 KB is a ~11x margin on that worst case
# and the same window `board_transcript._EXCHANGE_TAIL_BYTES` already uses for
# this file. `_ACTIVITY_TAIL_BYTES` (8 KB) is deliberately *not* reused: it
# would have missed the record in 6 of those 8 files.
_TITLE_TAIL_BYTES = 256 * 1024

# Outcomes of the live-title disproof below, reported as `title_check` on the
# drawer response. Four states, kept distinct on purpose: a check that cannot
# establish a fact reports that as its own state rather than folding into
# either answer.
#   corroborated - PTY title and the file's own declared name agree
#   disproved    - both present and genuinely conflicting; the scan is refused
#   unknown      - one side absent, so the question was asked and not settled
#   not_checked  - the question was never reached (route opted out, or the
#                  scan had already refused on one of its own two guards)
_TITLE_CHECK_CORROBORATED = "corroborated"
_TITLE_CHECK_DISPROVED = "disproved"
_TITLE_CHECK_UNKNOWN = "unknown"
_TITLE_CHECK_NOT_CHECKED = "not_checked"

# One info line per session whose scan the title disproved. The drawer
# re-polls every 5s while open and a title is stable for a session's life, so
# an unkeyed log would bury the line that matters — same shape as
# board_transcript.py's glyph breadcrumb.
_LOGGED_DISPROVED_SCANS: "set[str]" = set()
_DISPROVED_SCAN_LOG_CAP = 64
_CODEX_START_SLOP_SECONDS = 120

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
# A Claude Resume launch that names its conversation (#633's chief Resume,
# Life OS's conversation resume): `build_resume_flags` splices
# `--resume <session id>` into the launch flags. Only a UUID counts, so a
# bare `--resume` (the picker) followed by an ordinary flag never matches.
_CLAUDE_RESUME_ID_RE = re.compile(
    r"(?:^|\s)(?:--resume|-r)(?:\s+|=)"
    r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
    r"(?=\s|$)"
)
# Why the Claude filesystem fallback answered nothing (#1155), named in the
# `/transcript` route's `no_transcript` log line so the next report is
# diagnosable from the log alone.
_CLAUDE_REFUSED_UNCORRELATED = "no_dir_or_start"
_CLAUDE_REFUSED_SHARED_FOLDER = "folder_shared"
_CLAUDE_REFUSED_NO_FILE = "no_conversation_file"
_CLAUDE_REFUSED_STALE = "not_written_since_start"
_CLAUDE_REFUSED_TITLE = "title_disproved"


def find_codex_transcript(session: Dict[str, Any]) -> Optional[Path]:
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
    :func:`find_codex_transcript`, which has only cwd + launch time to go
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

    So this is the launch-window match :func:`find_codex_transcript` makes,
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


def _scan_claude_transcript(
    session: Dict[str, Any],
    live: Iterable[Dict[str, Any]],
    disprove: bool = False,
) -> Tuple[Optional[Path], str]:
    """:func:`_claude_scan` without the refusal reason: the drawer's view."""
    path, verdict, _refused = _claude_scan(session, live, disprove)
    return path, verdict


def _claude_project_folders(project_dir: str) -> Optional[List[Path]]:
    """The ``~/.claude/projects`` folders for a cwd, or None if unreadable.

    Built from the *normalized* directory so the session's own spelling —
    separator flavour, trailing slash, drive-letter case — cannot change the
    folder it looks for; matched case-insensitively for the legacy
    lowercased folders.
    """
    slug = _CLAUDE_SLUG_RE.sub("-", _normalize_dir(project_dir))
    try:
        return [
            child
            for child in _CLAUDE_PROJECTS_DIR.iterdir()
            if child.name.lower() == slug and child.is_dir()
        ]
    except OSError:
        return None


def _claude_scan(
    session: Dict[str, Any],
    live: Iterable[Dict[str, Any]],
    disprove: bool = False,
) -> Tuple[Optional[Path], str, str]:
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

    **The third guard, opt-in (#1034):** with ``disprove`` set, a chosen
    file is additionally checked against the PTY's own window title and
    *refused* when the two genuinely conflict — see
    :func:`_disprove_by_live_title`. It is opt-in because it is a ranking
    refinement the Board drawer wants and ``/transcript`` does not: there
    the scan has no competing source to outrank, so refusing would turn a
    rough answer into no answer at all. The verdict is returned alongside
    the path so the caller can report it;
    :func:`resolve_claude_transcript` is the unchanged two-state view for
    every caller that does not want the third guard.

    The third element names the guard that refused (``""`` on success), so
    a ``no_transcript`` answer can say why (#1155).
    """
    project_dir = str(session.get("project_dir") or "")
    started = _started_epoch(session)
    if not project_dir or started is None:
        return None, _TITLE_CHECK_NOT_CHECKED, _CLAUDE_REFUSED_UNCORRELATED
    normalized = _normalize_dir(project_dir)
    sid = str(session.get("session_id") or "")
    for other in live or ():
        if (
            str(other.get("session_id") or "") != sid
            and str(other.get("agent") or "claude").strip().lower() == "claude"
            and other.get("alive")
            and _normalize_dir(other.get("project_dir")) == normalized
        ):
            return None, _TITLE_CHECK_NOT_CHECKED, _CLAUDE_REFUSED_SHARED_FOLDER

    newest: Optional[Path] = None
    newest_at = 0.0
    folders = _claude_project_folders(project_dir)
    if folders is None:
        return None, _TITLE_CHECK_NOT_CHECKED, _CLAUDE_REFUSED_NO_FILE
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
        return None, _TITLE_CHECK_NOT_CHECKED, _CLAUDE_REFUSED_NO_FILE
    if newest_at < started - _CLAUDE_MTIME_SLOP_SECONDS:
        return None, _TITLE_CHECK_NOT_CHECKED, _CLAUDE_REFUSED_STALE
    if not disprove:
        return newest, _TITLE_CHECK_NOT_CHECKED, ""
    verdict = _disprove_by_live_title(newest, session)
    if verdict == _TITLE_CHECK_DISPROVED:
        return None, verdict, _CLAUDE_REFUSED_TITLE
    return newest, verdict, ""


def claude_resume_id(session: Dict[str, Any]) -> Optional[str]:
    """The conversation id a Claude session was launched to resume, if any.

    Read from the session's own launch ``flags``: ``--resume <id>`` names
    the conversation exactly. A bare ``--resume`` (the picker) and a fresh
    launch answer None: the user picks after spawn, and nothing on the
    session record says what they picked.
    """
    match = _CLAUDE_RESUME_ID_RE.search(str(session.get("flags") or ""))
    return match.group(1).lower() if match else None


def _claude_resumed_transcript(session: Dict[str, Any]) -> Optional[Path]:
    """``<cwd folder>/<resume id>.jsonl`` for a ``--resume <id>`` launch.

    Probed on Claude Code 2.1.280 (#1155): an interactive
    ``claude --resume <id>`` appends to that same ``<id>.jsonl`` rather than
    opening a new file, so the id names this session's conversation from
    spawn onwards, including before any prompt, when the hook row is gone
    and the scan's guards can refuse.
    """
    resume_id = claude_resume_id(session)
    project_dir = str(session.get("project_dir") or "")
    if not resume_id or not project_dir:
        return None
    for folder in _claude_project_folders(project_dir) or ():
        path = folder / f"{resume_id}.jsonl"
        if path.is_file():
            return path
    return None


def resolve_claude_transcript(
    session: Dict[str, Any], live: Iterable[Dict[str, Any]]
) -> Tuple[Optional[Path], str]:
    """``/transcript``'s row-less Claude resolution (#1023, #1155).

    The scan (two guards, no title disproof) answers first. When it refuses,
    a ``--resume <id>`` launch still names its conversation: the resumed
    session is a new process with a fresh ``started_at``, in a folder that
    may host other live sessions, so either guard can refuse a file that is
    exactly this session's. The id is exact, so neither guard applies to it,
    but one thing can still make it wrong: a ``/resume`` *inside* the
    session moves it to another conversation while its launch flags keep
    the old id. The live-title disproof (#1034) covers that: a title that
    genuinely conflicts with the resumed file's own name refuses it. A fresh
    launch and a bare ``--resume`` have no id and are unaffected.

    Returns ``(path, why)``: ``why`` is ``"scan"`` or ``"resume_id"`` for an
    answer, else the refusing guard, for the ``no_transcript`` log line.
    """
    path, _verdict, refused = _claude_scan(session, live)
    if path is not None:
        return path, "scan"
    resumed = _claude_resumed_transcript(session)
    if resumed is None:
        return None, refused
    if _disprove_by_live_title(resumed, session) == _TITLE_CHECK_DISPROVED:
        return None, f"{refused}; resume_id {_CLAUDE_REFUSED_TITLE}"
    return resumed, "resume_id"


def _claude_declared_titles(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """The newest ``custom-title`` and ``ai-title`` a conversation declares.

    Claude Code names a conversation in two independent records and both are
    in play: ``{"type": "ai-title", "aiTitle": ...}`` is the name it infers
    for itself, and ``{"type": "custom-title", "customTitle": ...}`` is an
    explicit one that overrides it in the window title. They are not
    interchangeable and they do drift apart — the standing fleet chief on
    this box carries ``customTitle: "chief"`` and
    ``aiTitle: "Investigate and fix failed jobs"`` in the *same* file, the
    ai-title frozen at what it inferred early (one distinct value across all
    38,094 lines) while the PTY paints the custom one. Reading only
    ``ai-title``, as #1034 first proposed, would have made the chief's own
    drawer disagree with itself permanently.

    Both are returned rather than resolved to one here, so the caller can
    accept a match against *either* — the fail-safe direction, since it can
    only ever refuse fewer answers.
    """
    custom: Optional[str] = None
    ai: Optional[str] = None
    for line in _read_tail(path, _TITLE_TAIL_BYTES).splitlines():
        # Cheap reject first: these records are a few per thousand lines and
        # the window is 256 KB of mostly message text.
        if '-title"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue  # a torn first line from the tail cut, or not a record
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "custom-title":
            value = str(obj.get("customTitle") or "").strip()
            if value:
                custom = value
        elif kind == "ai-title":
            value = str(obj.get("aiTitle") or "").strip()
            if value:
                ai = value
    return custom, ai


def _disprove_by_live_title(path: Path, session: Dict[str, Any]) -> str:
    """Use the PTY window title to *rule out* a scanned conversation (#1034).

    The gap this closes is the ``/resume`` window #1027 documented and left
    open: ``SessionEnd`` fires on ``/resume`` too, so a session that resumes
    a *different* conversation has no state row, and until the resumed
    conversation is itself written the newest file in the folder is the one
    it just **left**. Nothing about the two files separates them — the same
    process writes both, seconds apart — so no mtime guard can close it. The
    window title can, because Claude Code re-emits the resumed
    conversation's established title immediately, before any hook fires (the
    same property ``board_chief._reconcile_chief_label`` relies on): the
    title names what is on screen while the file names what was.

    **Disproof only, never confirmation.** A conflict refuses; nothing else
    changes anything. That direction is the whole safety argument — it can
    only make the resolver more conservative, so it cannot introduce a new
    way to show the wrong conversation. Used the other way round it would
    gate a working answer behind a signal that is absent in exactly the
    cases that need it most: a detached ``RemoteSession`` has no PTY and so
    no title at all, and those are the sessions that had no fallback
    whatsoever before #1027.

    Hence three outcomes rather than two, with ``unknown`` folded into
    neither: either side missing leaves the answer exactly as #1027 ranked
    it, and says so rather than implying the title agreed. Requiring *both*
    sides present and genuinely conflicting is also what keeps a title
    frozen by a wedged PTY (#636) from refusing a good answer by itself.
    """
    title = strip_status_glyph(session.get("live_title"))
    if not title:
        # No PTY, or none painted yet. A detached session lives here.
        return _TITLE_CHECK_UNKNOWN
    custom, ai = _claude_declared_titles(path)
    declared = [name for name in (custom, ai) if name]
    if not declared:
        # Claude Code names a conversation only once it has enough content to
        # name one, so a fresh or bootstrap-only conversation has neither
        # record — and silence is not disagreement.
        return _TITLE_CHECK_UNKNOWN
    if any(title.casefold() == name.casefold() for name in declared):
        return _TITLE_CHECK_CORROBORATED
    log_once(
        _LOGGED_DISPROVED_SCANS,
        str(session.get("session_id") or "") + "|" + title,
        _DISPROVED_SCAN_LOG_CAP,
        logger.info,
        "ℹ️ board: scanned transcript %s refused for session %s: "
        "PTY title %r matches neither declared name (custom=%r ai=%r); "
        "falling back to the launcher capture (#1034)",
        path.name, str(session.get("session_id") or "")[:8], title, custom, ai,
    )
    return _TITLE_CHECK_DISPROVED


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


def _read_tail(path: Path, n_bytes: int) -> str:
    return _read_tail_bytes(path, n_bytes).decode("utf-8", errors="replace")
