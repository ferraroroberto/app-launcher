"""Agent-aware conversation previews for the Board drawer (issue #457).

The hook row's Claude JSONL remains the best source when it exists because it
is structured chat data.  When no row names one, a Claude conversation is
correlated from the filesystem instead (#1027, reported as ``native_scan``
because the match is inferred rather than exact).  Launcher-owned PTYs also
have an exact-id capture, however, and that is the fallback for a scan that
refuses and for agents such as Codex that publish no hook transcript at all.

The capture is terminal output, not prose.  A bounded tail is replayed through
``pyte`` and reply blocks are selected by the same leading-bullet colour
contract as the browser's read-aloud extractor.  This keeps ANSI/full-screen
repaint bytes out of the API response and avoids any cwd-based guessing.

The filesystem correlation itself — locating which harness file belongs to a
live session at all, across Codex/Pi/Antigravity/Copilot and Claude's own
scan/resume-id fallbacks — lives in ``src/transcript_locate.py`` (#1309); this
module resolves *what to show*, not *which file*.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pyte

from src import plan_picker, transcript_locate
from src.board_transcript import _read_tail_bytes, _tail_lines, last_exchange
from src.transcript_flavors.codex import codex_entries

logger = logging.getLogger(__name__)

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
    live: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resolve one live session's exchange through the source hierarchy.

    The row-declared ``native_path`` is exact and always wins. When no row
    names one and the agent is Claude,
    :func:`transcript_locate._scan_claude_transcript`
    (#1023, the scan ``/transcript`` shares via
    :func:`transcript_locate.resolve_claude_transcript`) correlates the
    conversation from the filesystem — the fallback for the window where
    the hook has deleted the row out from under a still-live session.

    **Ordering (#1027).** Unlike ``/transcript``, this route already had a
    working fallback, so adding a second one is a ranking decision rather
    than a gap-fill. The scanned JSONL is placed **above** the launcher's
    exact-id PTY capture, for two reasons: it is structured chat data
    rather than replayed terminal output, and a *detached* session has no
    capture at all, so for a ``RemoteSession`` it is the difference between
    an exchange and ``no_exchange``. The capture is kept for when the scan
    refuses — either of :func:`transcript_locate._scan_claude_transcript`'s
    two guards — so an ambiguous folder still degrades to the
    rougher-but-honest answer rather than to a neighbour's text.

    **The ranking's one cost, and how it is now bounded (#1034).** Claude
    Code fires ``SessionEnd`` on ``/resume`` too, so the rowless window
    opens the moment a session resumes a *different* conversation. Until
    the resumed conversation is itself written, the newest file in the
    folder is the one the session just left, and the scan answered with it
    while the capture showed what is on screen now. Nothing on the
    filesystem separates the two — both are written by the same process
    seconds apart — so no tightening of the mtime guard closes this
    window, and none was added rather than ship a knob that only looks
    like a fix. What does close it is a signal from *outside* the
    filesystem: the PTY's own window title, which Claude Code repaints
    with the resumed conversation's name before any hook fires. When it
    and the scanned file's own declared name are both present and
    genuinely conflict, the scan is refused and the capture answers
    instead (:func:`transcript_locate._disprove_by_live_title`). It
    disproves only, never confirms, so it can make this resolver more
    conservative and never less — and when it cannot settle the question
    it says so as its own ``title_check`` state rather than passing for
    agreement.

    Because that correlation is *inferred* and not established, it reports
    itself as its own source, ``native_scan``, rather than folding into the
    exact ``native``: a check that cannot establish a fact must not be
    scored as the passing state. ``/transcript`` keeps reporting ``native``
    for both, deliberately — there it has no competing source to outrank,
    so it has nothing to be honest about.

    Every response whose resolution went through that scan also carries
    ``title_check`` — ``corroborated``, ``disproved``, ``unknown`` or
    ``not_checked`` — so the drawer and the logs can tell "the title
    agreed" from "there was no title to ask". A check that cannot
    establish a fact reports that as its own state instead of folding
    into the answer it was meant to qualify.
    """
    native = last_exchange(native_path)
    if native.get("available"):
        return {**native, "source": "native", "reason": None}

    agent = str(session.get("agent") or "claude").lower()
    title_check: Optional[str] = None
    if not native_path and agent == "claude":
        scanned_path, title_check = transcript_locate._scan_claude_transcript(
            session, live or (), disprove=True
        )
        scanned = last_exchange(scanned_path)
        if scanned.get("available"):
            return {
                **scanned,
                "source": "native_scan",
                "reason": None,
                "title_check": title_check,
            }

    if agent == "codex":
        codex_path = transcript_locate.find_codex_transcript(session)
        codex = codex_last_exchange(codex_path)
        if codex.get("available"):
            return codex

    fallback = launcher_last_exchange(
        launcher_capture_path,
        launcher_input_path=launcher_input_path,
        prompt_fallback=str(session.get("prompt_title") or "").strip(),
        rows=int(session.get("rows") or 40),
        cols=int(session.get("cols") or 120),
    )
    if fallback.get("available"):
        return _with_title_check(fallback, title_check)

    native_declared = bool(native_path)
    capture_exists = _nonempty_file(launcher_capture_path)
    if capture_exists:
        reason = str(fallback.get("reason") or "capture_unparseable")
    elif native_declared:
        reason = "native_unavailable"
    else:
        reason = "no_exchange"
    return _with_title_check(unavailable(reason), title_check)


def _with_title_check(
    result: Dict[str, Any], title_check: Optional[str]
) -> Dict[str, Any]:
    """Attach the scan's ``title_check`` verdict to a non-scan answer.

    Only when the Claude scan actually ran (``title_check`` is ``None`` for
    an exact row, a non-Claude agent, or any route that never reached the
    scan), so the field's presence means "the scan was consulted" and its
    value says what the title settled. The ``disproved`` case is the one
    that matters most here: it is the only reason a launcher capture can be
    answering while a readable scanned conversation sat right there, and
    without the field that would be indistinguishable from the scan simply
    finding nothing.
    """
    if title_check is None:
        return result
    return {**result, "title_check": title_check}


def codex_last_exchange(path: Optional[Path]) -> Dict[str, Any]:
    """Read the newest Codex user/assistant messages from bounded JSONL.

    Reads the rollout through :func:`transcript_flavors.codex.codex_entries`
    (#1422) so the drawer and the Chat reader agree on what counts as a typed
    prompt — harness plumbing such as ``<environment_context>`` is a ``system``
    entry there, never the "last prompt" shown here.
    """
    if path is None:
        return unavailable("native_unavailable")
    raw = _read_tail(path, _CODEX_TAIL_BYTES)
    if not raw:
        return unavailable("native_unavailable")
    entries = codex_entries(list(enumerate(raw.splitlines())), uncapped=True)
    messages = [e for e in entries if e["kind"] in ("user", "assistant")]
    assistant_index = next(
        (index for index in range(len(messages) - 1, -1, -1)
         if messages[index]["kind"] == "assistant"),
        None,
    )
    if assistant_index is None:
        return unavailable("no_exchange")
    user_record = next(
        (messages[index] for index in range(assistant_index - 1, -1, -1)
         if messages[index]["kind"] == "user"),
        None,
    )
    assistant = messages[assistant_index]
    return {
        "available": True,
        "source": "codex",
        "reason": None,
        "user": (
            {
                "text": user_record["text"][-_USER_TEXT_CAP:],
                "timestamp": user_record["timestamp"],
            }
            if user_record else None
        ),
        "assistant": {
            "text": assistant["text"][-_ASSISTANT_TEXT_CAP:],
            "timestamp": assistant["timestamp"],
        },
    }


def launcher_last_exchange(
    capture_path: Path,
    *,
    launcher_input_path: Optional[Path] = None,
    prompt_fallback: str = "",
    rows: int = 40,
    cols: int = 120,
) -> Dict[str, Any]:
    """Extract the latest reply from an exact-id PTY capture tail.

    The tail is read through :func:`plan_picker.read_capture_tail`, which undoes
    the session-host's text-mode CRLF expansion and starts the render after the
    first newline; only the scrollback-aware, colour-reading render below is
    local, because the shared ``screen_lines`` keeps neither history nor cells.
    """
    raw = plan_picker.read_capture_tail(capture_path, _CAPTURE_TAIL_BYTES)
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
