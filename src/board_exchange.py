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

from src.board_transcript import _read_tail_bytes, last_exchange

_CAPTURE_TAIL_BYTES = 512 * 1024
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
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
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
