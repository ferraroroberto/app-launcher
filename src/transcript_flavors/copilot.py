"""GitHub Copilot CLI ``events.jsonl`` line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.transcript_flavors import _shared
from src.transcript_flavors._shared import (
    Entry,
    Line,
    NO_CAP,
    SYSTEM_TEXT_CAP,
    THINKING_TEXT_CAP,
    _attach_result,
    _entry,
    _loads,
    _text_entry,
    _tool_action,
    _tool_summary,
    _with_action,
)

# Copilot's own bookkeeping event families, none of which carry conversation
# the reader should render. `model.*` is the worst of them: `model.message`,
# `model.response` and `model.messages_snapshot` each re-serialise the whole
# conversation so far, so a reader keying on a nested "content" anywhere
# would multiply every turn. Listed here for the docstring's sake —
# `copilot_entries` matches on the exact types it wants and drops the rest,
# which is what keeps a newly-added event family from ever being rendered.
_COPILOT_PLUMBING = (
    "model.", "hook.", "assistant.turn_", "session.", "system.message",
)


def _copilot_result_text(result: Any) -> str:
    """The text of a ``tool.execution_complete`` result.

    ``result`` is a dict of ``content`` (+ an optional ``detailedContent``)
    on 25 of 27 completions on this box, and ``content``-only on the other
    two. The two fields are **different views, not a short/long pair** — on
    the three completions where they differ, ``content`` held a file's
    numbered text while ``detailedContent`` held a diff of it, and on one of
    them ``content`` was the longer. So ``content`` — what the model itself
    was given — is the card's text, and ``detailedContent`` is only a
    fallback for a record lacking it. A string result is used as-is; a
    non-textual one becomes a placeholder rather than a dump of its repr.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("content", "detailedContent"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return "[non-text result]" if result else ""
    if result is None:
        return ""
    return "[non-text result]"


def _copilot_outcome(data: Dict[str, Any]) -> Tuple[str, bool]:
    """A ``tool.execution_complete``'s card text and whether it failed (#1020).

    Copilot has **two** outcome fields, at two different levels, and only
    both together cover a failure:

    * ``success`` is about the *tool*. Measured: a ``view`` of a missing
      path writes ``success: false`` with an ``error`` object
      (``{"message": "Path does not exist", "code": "failure"}``) and
      **no** ``result`` key at all — so the reader has to take its card
      text from ``error.message``, or the failed call renders blank.
    * ``shellExecution.exitCode`` is about the *command*. A shell call whose
      command exits non-zero still reports ``success: true`` (measured:
      ``git rev-parse --short NO_SUCH_REF_xyz`` → ``success: true``,
      ``shellExecution: {"exitCode": 1}``), so the tool-level flag alone
      would read that as a clean run.

    The gap that keeps this flavour ``partial`` rather than ``reported``:
    only 2 of 27 completions in the on-disk corpus carry ``shellExecution``
    at all, so a shell call without it leaves its command's exit
    **unrecorded** — the harness's own trailing
    ``<shellId: N completed with exit code 1>`` footer is the only trace,
    and reading an outcome out of result prose is a rule no reader here
    makes (see :data:`session_transcript.FLAVORS`).
    """
    error = data.get("error")
    if data.get("success") is False:
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
        elif isinstance(error, str):
            message = error.strip()
        return (message or _copilot_result_text(data.get("result"))
                or "[tool failed, no message]"), True
    shell = data.get("shellExecution")
    code = shell.get("exitCode") if isinstance(shell, dict) else None
    failed = isinstance(code, int) and not isinstance(code, bool) and code != 0
    return _copilot_result_text(data.get("result")), failed


def copilot_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from a GitHub Copilot CLI ``events.jsonl``, in file order.

    One self-contained JSON event per line — ``{"type", "data", "id",
    "parentId", "timestamp"}`` — so no entry ever needs a neighbouring line
    (:func:`_copilot_line_key`) and a line yields at most one turn, which is
    what :func:`session_transcript.entry_full_text` resolves against.
    Measured on a session launched through the launcher's own API for #1015,
    plus the 37 event logs already on disk:

    * A typed prompt is ``user.message``'s ``data.content``. Its sibling
      ``transformedContent`` is the same text wrapped in a
      ``<current_datetime>`` block the model sees and the user never typed,
      so it is not what a card shows.
    * ``assistant.message`` carries ``data.content`` **and**, when the model
      called a tool, ``data.toolRequests``. The tool call is deliberately
      **not** taken from there: the following ``tool.execution_start`` line
      carries the same call with its real arguments, and emitting both would
      double every tool card. A tool-calling message usually has no
      ``content`` at all (5 of 5 in the capture) but may carry narration;
      either way its text is emitted at its own offset and the call comes
      from a *later* line, so a reply can never be hoisted above the call
      that produced it (the trap #1012 hit).
    * **Call and result pair by an exact id** — ``data.toolCallId`` on both
      ``tool.execution_start`` and ``tool.execution_complete`` — so unlike
      Antigravity (#1014) there is no positional guessing. A result whose
      call fell off the page stands alone, as in every other flavour.
    * ``data.reasoningText`` is readable thinking and folds as ``thinking``
      (14 of 68 assistant messages here). Its neighbours
      ``reasoningOpaque``, ``encryptedContent`` and ``reasoningBlocks``
      (52, 44 and 7 of them) are **encrypted provider blobs** and are never
      read — a card must not show a screenful of base64.
    * ``session.error`` folds as ``system``: it is where the launcher's
      rejected ``--model`` flag surfaces (#1017), worth seeing folded
      rather than dropping silently.
    * Everything else is plumbing (:data:`_COPILOT_PLUMBING`), dropped by
      not being matched.
    * Timestamps are ISO-8601 ``Z`` strings on the event envelope, which the
      client's ``new Date`` reads directly — no epoch conversion, so none of
      #1012's 1970 cards.

    **This is by far the least byte-efficient of the five flavours.** The
    capture ran 670 KB for three short turns — ~220 KB per turn, of which
    about 600 bytes was visible text. The bulk is ``model.*`` bookkeeping
    (70%) plus one ~87 KB ``system.message`` holding the whole system prompt
    (written once per session here, not per turn), and a single
    ``assistant.message`` line reaches 14 KB because of its opaque blobs.
    The paging contract absorbs this exactly as designed —
    :data:`session_transcript.REQUEST_BYTE_CAP` ends a page early and hands
    back a cursor — but a Copilot page carries fewer turns per request than
    the other flavours, so "Load older" is tapped more often. Nothing is
    lost, and the system prompt never reaches a card.

    Known omission, shared with the Codex, Grok, Pi and Antigravity
    flavours (#1020): a failed tool call reads much like a successful one.
    Copilot's ``data.success`` describes whether the *tool* ran, not whether
    the command succeeded — a shell command exiting 1 is still
    ``success: true``, with the failure only in the result's own
    ``<shellId: N completed with exit code 1>`` footer. That footer is kept
    in the result text precisely because it is the only signal there is.

    ``uncapped`` — see :func:`src.transcript_flavors.claude.claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else _shared.ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else _shared.USER_TEXT_CAP
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None:
            continue
        kind = obj.get("type")
        ts = obj.get("timestamp")
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}

        if kind == "user.message":
            text = str(data.get("content") or "")
            if text.strip():
                entries.append(_text_entry(
                    "user", offset, ts, text, user_cap, sidechain=False))
            continue

        if kind == "assistant.message":
            thinking = str(data.get("reasoningText") or "").strip()
            if thinking:
                entries.append(_text_entry(
                    "thinking", offset, ts, thinking, THINKING_TEXT_CAP,
                    sidechain=False))
            text = str(data.get("content") or "")
            if text.strip():
                entries.append(_text_entry(
                    "assistant", offset, ts, text, assistant_cap, sidechain=False))
            continue

        if kind == "tool.execution_start":
            call_id = str(data.get("toolCallId") or "")
            entry = _with_action(_entry(
                "tool_call", offset, ts,
                name=str(data.get("toolName") or "tool"),
                summary=_tool_summary(data.get("arguments")),
                result=None, result_truncated=False, sidechain=False,
            ), _tool_action(str(data.get("toolName") or ""), data.get("arguments")))
            entries.append(entry)
            if call_id:
                open_calls[call_id] = entry
            continue

        if kind == "tool.execution_complete":
            text, failed = _copilot_outcome(data)
            _attach_result(
                entries, open_calls, data.get("toolCallId"),
                text, offset, ts, False, error=failed,
            )
            continue

        if kind == "session.error":
            text = str(data.get("message") or "").strip()
            if text:
                entries.append(_text_entry(
                    "system", offset, ts, text, SYSTEM_TEXT_CAP,
                    label=str(data.get("errorType") or "error"), sidechain=False))
    return entries


def _copilot_line_key(raw: str) -> Optional[str]:
    """Copilot writes one self-contained event per line (see
    :func:`copilot_entries`), so no line ever needs another."""
    return None
