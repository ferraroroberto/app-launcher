"""Claude Code's plan picker, read off the live terminal screen (#1151).

The Chat pane's plan card can't answer from the transcript alone. Claude Code
can keep the pending ``ExitPlanMode`` call off disk until it is answered
(measured: 31 s with the picker up and the call not written), and the
picker's first option depends on the session's permission mode: "Yes,
auto-accept edits" in a ``--permission-mode plan`` session, "Yes, and switch
to BYPASS PERMISSIONS (no further prompts) for this session" in a
``--dangerously-skip-permissions`` one. So the card answers only what the
screen shows. The server renders the session's PTY capture through ``pyte``
at the PTY's own size, reads the picker's options off it, and reads it again
right before typing, so a tap is only sent while the digit still means the
label the user tapped.

What the picker looks like (probed on Claude Code 2.1.281, plan-mode and
bypass sessions, 120 and 46 columns)::

    ────────────────────────────────────────
     Claude has written up a plan and is ready to
     execute. Would you like to proceed?

     ❯ 1. Yes, and switch to BYPASS PERMISSIONS
          (no further prompts) for this session
       2. Yes, manually approve edits
       3. Tell Claude what to change
          shift+tab to approve with this feedback

     ctrl+g to edit in Notepad · C:\\...\\.claude\\
     plans\\<name>.md

Keys, as probed: a digit on a "Yes" option approves at once, with no Enter.
The feedback option's digit focuses its text field (its label becomes what
is typed), the text follows as ordinary keys, and Enter sends the plan back.
While that field has focus a digit is text, so nothing is answerable from
Chat until the terminal leaves it.

Pure apart from :func:`read_capture_tail`. Whatever this module can't
recognise reads as "not showing", and then Chat sends nothing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pyte

from src.ask_user_question import FREE_TEXT_MAX, Keystroke

# Enough capture to hold the picker and a long plan above it. The tail is
# rendered at the PTY's own size, so only its last screenful counts; 96 KB
# renders in well under 0.1 s.
CAPTURE_TAIL_BYTES = 96 * 1024
PLAN_CAP = 12_000
LABEL_CAP = 200

QUESTION = "Would you like to proceed?"
PLAN_HEADING = "Here is Claude's plan:"
FEEDBACK_HINT = "shift+tab to approve with this feedback"
# The feedback option's own label while its field is empty. Once text is
# typed into it the label *is* that text, and a send from Chat would append
# to it, so any other label on that option makes it untappable.
FEEDBACK_PLACEHOLDER = "Tell Claude what to change"
FOOTER_PREFIX = "ctrl+g"

APPROVE = "approve"
FEEDBACK = "feedback"
OTHER = "other"

_CURSOR = "\u276f"  # ❯
# The picker's frame is a solid rule; the plan inside it sits between two
# dashed ones.
_RULES = frozenset({"\u2500", "\u254c"})  # ─ ╌
_OPTION_RE = re.compile(r"^(\s*)(" + _CURSOR + r"\s+)?(\d)\.\s+(\S.*)$")
_PLAN_FILE_RE = re.compile(r"plans[\\/]([A-Za-z0-9._-]+\.md)")


def read_capture_tail(path: Path, n_bytes: int = CAPTURE_TAIL_BYTES) -> Optional[str]:
    """The last ``n_bytes`` of a session's PTY capture, or ``None`` when the
    file can't be read.

    The session-host writes the capture in text mode, so on Windows every
    ``\\n`` the agent printed is on disk as ``\\r\\n`` (a ``\\r\\n`` as
    ``\\r\\r\\n``). Replacing each ``\\r\\n`` with ``\\n`` undoes that exactly;
    reading in text mode would not, since it also turns a lone ``\\r`` into
    a newline. The cut starts after the first newline so the render never
    begins inside an escape sequence.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            raw = fh.read()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if os.linesep != "\n":
        text = text.replace(os.linesep, "\n")
    if size > n_bytes:
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    return text


def screen_lines(text: str, rows: int, cols: int) -> List[str]:
    """``text`` rendered on a ``cols`` x ``rows`` screen, one string a row."""
    screen = pyte.Screen(max(20, int(cols)), max(5, int(rows)))
    pyte.Stream(screen).feed(text)
    return [line.rstrip() for line in screen.display]


def _is_rule(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= _RULES


def _paragraphs(lines: List[str]) -> List[Tuple[int, int, str]]:
    """``(first, last, joined text)`` for each run of non-blank lines."""
    out: List[Tuple[int, int, str]] = []
    start: Optional[int] = None
    for i, line in enumerate(lines + [""]):
        if line.strip() and start is None:
            start = i
        elif not line.strip() and start is not None:
            out.append((start, i - 1, " ".join(x.strip() for x in lines[start:i])))
            start = None
    return out


def _norm(label: str) -> str:
    return " ".join(str(label or "").split())


def parse_picker(lines: List[str]) -> Optional[Dict[str, Any]]:
    """The plan picker on this screen, or ``None`` when it isn't showing.

    Returns ``{"options": [{"n", "label", "kind"}], "cursor": n | None,
    "answerable": bool, "plan_file": name | None, "plan_excerpt": str}``.

    Strict on purpose: the question paragraph, then options numbered 1..n
    with a feedback option among them, then nothing but blank rows and the
    ``ctrl+g`` footer to the bottom of the screen. Anything else below the
    options (a prompt, a status line, a new reply) means the picker is not
    what the terminal is showing now.
    """
    paras = _paragraphs(lines)
    question = next((p for p in reversed(paras) if QUESTION in p[2]), None)
    if question is None:
        return None
    options: List[Dict[str, Any]] = []
    indent = 0
    i = question[1] + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _OPTION_RE.match(line)
        if m:
            label_col = m.start(4)
            options.append({
                "n": int(m.group(3)), "parts": [m.group(4).strip()],
                "cursor": bool(m.group(2)), "hint": False,
            })
            indent = label_col
            i += 1
            continue
        lead = len(line) - len(line.lstrip())
        if options and lead >= indent:
            if line.strip().startswith("shift+tab"):
                options[-1]["hint"] = True
            else:
                options[-1]["parts"].append(line.strip())
            i += 1
            continue
        break
    if len(options) < 2 or [o["n"] for o in options] != list(range(1, len(options) + 1)):
        return None
    if not any(o["hint"] for o in options):
        return None
    footer: List[str] = []
    for line in lines[i:]:
        if not line.strip():
            if footer:
                footer.append("")
            continue
        if not footer and not line.strip().startswith(FOOTER_PREFIX):
            return None
        if footer and footer[-1] == "":
            return None  # a second paragraph below the footer
        footer.append(line.strip())

    out_opts = []
    cursor: Optional[int] = None
    for o in options:
        label = " ".join(o["parts"])[:LABEL_CAP]
        if o["hint"]:
            kind = FEEDBACK if _norm(label) == FEEDBACK_PLACEHOLDER else OTHER
        else:
            kind = APPROVE if label.startswith("Yes") else OTHER
        if o["cursor"]:
            cursor = o["n"]
        out_opts.append({"n": o["n"], "label": label, "kind": kind, "field": o["hint"]})
    # With the feedback field focused (the cursor on it), a digit is text.
    focused = next((o for o in out_opts if o["n"] == cursor), None)
    answerable = focused is not None and not focused["field"]
    m = _PLAN_FILE_RE.search("".join(x for x in footer if x))
    return {
        "options": [{k: o[k] for k in ("n", "label", "kind")} for o in out_opts],
        "cursor": cursor,
        "answerable": answerable,
        "plan_file": m.group(1) if m else None,
        "plan_excerpt": _plan_excerpt(lines, question[0]),
    }


def _plan_excerpt(lines: List[str], question_row: int) -> str:
    """The plan as the terminal shows it: the rows between the rule under
    "Here is Claude's plan:" and the next rule. ``""`` when it isn't all on
    screen (a plan taller than the terminal)."""
    head = next((i for i in range(question_row - 1, -1, -1) if PLAN_HEADING in lines[i]), None)
    if head is None:
        return ""
    top = next((i for i in range(head + 1, question_row) if _is_rule(lines[i])), None)
    if top is None:
        return ""
    end = next((i for i in range(top + 1, question_row) if _is_rule(lines[i])), None)
    if end is None:
        return ""
    body = [line[1:] if line.startswith(" ") else line for line in lines[top + 1:end]]
    return "\n".join(body).strip("\n")[:PLAN_CAP]


def read_plan_file(name: Optional[str]) -> Optional[Tuple[str, bool]]:
    """``(markdown, truncated)`` from the plan file the footer names, looked
    up by its bare name in Claude Code's own plans folder only — never a
    path taken from the screen. ``None`` when there is none to read."""
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]+\.md", name):
        return None
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    path = Path(base) / "plans" / name
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    return text[:PLAN_CAP], len(text) > PLAN_CAP


def answer_keys(picker: Optional[Dict[str, Any]], option: Any, label: Any,
                feedback: Any = None) -> List[Keystroke]:
    """The keys for one tap, checked against ``picker`` (the screen read at
    send time, never the client's copy).

    Raises :class:`PickerChanged` when the screen no longer shows what the
    user tapped, :class:`ValueError` for a malformed request. Either way no
    key is built, so nothing half-typed can reach the picker.
    """
    if not isinstance(option, int) or isinstance(option, bool):
        raise ValueError("option must be a number")
    if picker is None:
        raise PickerChanged("Chat can't see the plan picker on the terminal, so nothing was sent")
    if not picker.get("answerable"):
        raise PickerChanged("The terminal is in the middle of an answer: finish it there")
    chosen = next((o for o in picker["options"] if o["n"] == option), None)
    if chosen is None or _norm(chosen["label"]) != _norm(label):
        raise PickerChanged("The plan's options changed on the terminal: nothing was sent, check them again")
    if chosen["kind"] == APPROVE:
        return [(str(option), False)]
    if chosen["kind"] != FEEDBACK:
        raise PickerChanged("That option can only be answered in the terminal")
    text = " ".join(str(feedback or "").split())
    if not text:
        raise ValueError("say what to change before sending the plan back")
    if len(text) > FREE_TEXT_MAX:
        raise ValueError(f"feedback is over {FREE_TEXT_MAX} characters")
    return [(str(option), False), (text, True)]


class PickerChanged(Exception):
    """The screen doesn't show what the tap was for: a 409, never a retry."""
