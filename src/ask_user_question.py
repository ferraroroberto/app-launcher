"""Claude Code's ``AskUserQuestion`` tool, for the Chat pane (#1149).

Two halves, both pure:

* **Reading.** :func:`questions_from_input` / :func:`answers_from_result`
  lift the tool's structured payload out of a transcript line, sanitized and
  capped, so the Chat pane can render a question card instead of a folded
  ``AskUserQuestion`` / ``questions`` row. The input is the agent's own tool
  call (``{"questions": [{question, header, multiSelect, options: [{label,
  description}]}]}``); the answer is the ``toolUseResult.answers`` dict Claude
  Code writes beside the ``tool_result`` block — question text to the chosen
  label, several labels joined by ``", "`` for a multi-select, or the typed
  text for "Type something" (characterized across 67 answered and 19
  declined calls on the dev box; a declined call carries the string
  ``"User rejected tool use"`` instead, with ``is_error``).

* **Answering.** :func:`answer_keystrokes` turns a Chat pick into the
  keystrokes Claude Code's picker takes. Probed against a live Claude Code
  2.1.281 PTY and a detached console (evidence on the issue):

  - A digit is a **jump-select**: on a single-select question it picks that
    option *and* moves on — no Enter. On a multi-select one it toggles that
    option; Tab moves to the next question (or the Submit tab).
  - Option ``n + 1`` is "Type something": its digit focuses an inline text
    field, the text follows as ordinary keys, Enter confirms it.
  - More than one question, or any multi-select, ends on a "Review your
    answers" tab whose option ``1`` is "Submit answers".
  - The keys must arrive as **raw keystrokes**. The ``/input`` route frames
    text as a bracketed paste whenever the TUI has that mode on, and the
    picker ignores a pasted digit — the Enter ``/input`` sends after it then
    confirms whatever the cursor was on (the probe answered option 1 to a
    "2"). Hence a route of its own rather than ``/input``.
  - Each key in its own write: the picker reads one keystroke per input
    event, so ``"13"`` in one write is not "1 then 3".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

TOOL_NAME = "AskUserQuestion"

# Display caps. The tool's own schema bounds the shape (1-4 questions, 2-4
# options each), so these only stop a pathological payload from bloating a
# transcript page.
MAX_QUESTIONS = 4
MAX_OPTIONS = 8
QUESTION_CAP = 1_000
HEADER_CAP = 60
LABEL_CAP = 200
DESCRIPTION_CAP = 600
ANSWER_CAP = 1_000
# A typed answer goes to the agent as keystrokes: long enough for a real
# answer, short enough that a stuck key loop cannot flood the picker.
FREE_TEXT_MAX = 500

# One step of an answer: ``(text, enter)`` — the text as raw keys, then an
# Enter as its own write when ``enter`` is true.
Keystroke = Tuple[str, bool]


def _text(value: Any, cap: int) -> str:
    return str(value or "").strip()[:cap] if isinstance(value, (str, int, float)) else ""


def questions_from_input(inputs: Any) -> Optional[List[Dict[str, Any]]]:
    """The tool input's questions, sanitized for display, or ``None`` when
    the input isn't the shape this reader knows — the caller then keeps the
    generic tool row, so an encoding change can never break the pane."""
    if not isinstance(inputs, dict) or not isinstance(inputs.get("questions"), list):
        return None
    out: List[Dict[str, Any]] = []
    for q in inputs["questions"][:MAX_QUESTIONS]:
        if not isinstance(q, dict):
            return None
        options = q.get("options")
        if not isinstance(options, list) or not options:
            return None
        clean = []
        for opt in options[:MAX_OPTIONS]:
            if not isinstance(opt, dict) or not _text(opt.get("label"), LABEL_CAP):
                return None
            clean.append({
                "label": _text(opt.get("label"), LABEL_CAP),
                "description": _text(opt.get("description"), DESCRIPTION_CAP),
            })
        out.append({
            "question": _text(q.get("question"), QUESTION_CAP),
            "header": _text(q.get("header"), HEADER_CAP),
            "multiSelect": bool(q.get("multiSelect")),
            "options": clean,
        })
    return out or None


def answers_from_result(tool_use_result: Any) -> Optional[Dict[str, str]]:
    """``toolUseResult.answers`` sanitized, or ``None`` when the line carries
    none (a declined call, another tool, an unknown encoding)."""
    if not isinstance(tool_use_result, dict):
        return None
    answers = tool_use_result.get("answers")
    if not isinstance(answers, dict):
        return None
    out = {
        _text(k, QUESTION_CAP): _text(v, ANSWER_CAP)
        for k, v in answers.items()
        if isinstance(k, str)
    }
    return out or None


def answer_keystrokes(questions: Any, answers: Any) -> List[Keystroke]:
    """The picker keystrokes for one answer per question, in order.

    ``questions`` is the call's own input (``input["questions"]`` as the agent
    wrote it — the server's copy, never the client's); ``answers`` has one
    entry per question: ``{"option": n}`` (1-based) or ``{"text": str}`` for a
    single-select question, ``{"options": [n, ...]}`` for a multi-select one.
    Typed text on a multi-select question is refused: its toggle-then-type
    sequence was not probed.

    Raises :class:`ValueError` with a user-facing reason for anything else,
    before a single key is built — a malformed answer must never become a
    half-typed one.
    """
    if not isinstance(questions, list) or not questions:
        raise ValueError("the question has no options to answer")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError(f"expected {len(questions)} answer(s), got "
                         f"{len(answers) if isinstance(answers, list) else 0}")
    keys: List[Keystroke] = []
    review = len(questions) > 1
    for i, (q, a) in enumerate(zip(questions, answers), start=1):
        options = q.get("options") if isinstance(q, dict) else None
        if not isinstance(options, list) or not options:
            raise ValueError(f"question {i} has no options")
        if not isinstance(a, dict):
            raise ValueError(f"answer {i} is not an object")
        count = len(options)
        # A digit is one keystroke: "10" would be "1" then "0".
        if count + 1 > 9:
            raise ValueError(f"question {i} has too many options to pick by number")
        if q.get("multiSelect"):
            review = True
            picks = a.get("options")
            if not isinstance(picks, list) or not picks:
                raise ValueError(f"pick at least one option for question {i}")
            if any(not _valid_option(n, count) for n in picks) or len(set(picks)) != len(picks):
                raise ValueError(f"question {i} has an option out of range")
            keys.extend((str(n), False) for n in sorted(picks))
            keys.append(("\t", False))
            continue
        if "text" in a:
            text = " ".join(str(a.get("text") or "").split())
            if not text:
                raise ValueError(f"the typed answer for question {i} is empty")
            if len(text) > FREE_TEXT_MAX:
                raise ValueError(f"the typed answer for question {i} is over {FREE_TEXT_MAX} characters")
            keys.append((str(count + 1), False))
            keys.append((text, True))
            continue
        n = a.get("option")
        if not _valid_option(n, count):
            raise ValueError(f"question {i} has an option out of range")
        keys.append((str(n), False))
    if review:
        keys.append(("1", False))
    return keys


def _valid_option(n: Any, count: int) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= count
