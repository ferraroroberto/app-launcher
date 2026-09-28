"""Antigravity CLI conversation-log line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

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

# Antigravity's `SYSTEM`-sourced steps are harness plumbing; the label is
# what the folded `system` card shows. A step type absent here still folds,
# under the generic label, so a new one is never rendered as a reply.
_AGY_SYSTEM_LABELS = {
    "EPHEMERAL_MESSAGE": "ephemeral",
    "SYSTEM_MESSAGE": "system-message",
    "CHECKPOINT": "compaction",
    "ERROR_MESSAGE": "error",
    "CONVERSATION_HISTORY": "history",
}
# A typed prompt arrives wrapped, followed by `<ADDITIONAL_METADATA>` (local
# time) and sometimes `<USER_SETTINGS_CHANGE>` — only the request is shown.
_AGY_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
# Tool-argument keys that are Antigravity's own UI labels rather than
# arguments ("Listing directory contents"), so they never stand in for the
# command or path a tool card should show.
_AGY_LABEL_ARGS = ("toolAction", "toolSummary")
# Every tool result opens with the harness's own timing header — 430 of 430
# on this box — which is two lines of boilerplate on a phone card and two
# lines of the result cap. The `Completed At` line is absent while a tool is
# still running.
_AGY_RESULT_HEADER_RE = re.compile(
    r"\ACreated At: [^\n]*\n(?:Completed At: [^\n]*\n)?"
)


def _agy_arg(value: Any) -> Any:
    """One tool argument, undoing ``transcript.jsonl``'s second JSON encode.

    The flat file writes each argument's value re-encoded (``"\\"C:\\\\\\\\x\\""``)
    while ``transcript_full.jsonl`` writes it plain; a reader that hits the
    fallback file would otherwise show the quotes and doubled backslashes.
    """
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _agy_tool_summary(args: Any) -> str:
    """A tool call's one-line summary: the command, path or prompt.

    :func:`src.transcript_flavors._shared._tool_summary`'s first-string-argument
    rule, applied after the UI labels are dropped — so a card reads
    ``run_command`` / the command line, as the Claude and Pi flavours do,
    rather than ``File type check``. The labels are the fallback for a call
    whose real arguments are all non-strings.
    """
    if not isinstance(args, dict):
        return _tool_summary(_agy_arg(args))
    unwrapped = {key: _agy_arg(value) for key, value in args.items()}
    labels = {key: unwrapped.pop(key, "") for key in _AGY_LABEL_ARGS}
    if any(isinstance(value, str) and value.strip() for value in unwrapped.values()):
        return _tool_summary(unwrapped)
    return _tool_summary(_agy_arg(labels.get("toolSummary"))) or _tool_summary(unwrapped)


def _agy_mark(entry: Entry, field: str, cut: Any) -> Entry:
    """Flag ``entry`` truncated when the harness itself cut ``field``
    (``truncated_fields``, written only by the fallback flat file)."""
    if field in cut:
        entry["truncated"] = True
    return entry


def antigravity_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from an Antigravity CLI conversation log, in file order.

    One JSON step per line — ``{"step_index", "source", "type", "status",
    "created_at", "content"?, "thinking"?, "tool_calls"?}`` — so no entry
    ever needs a neighbouring line (:func:`_antigravity_line_key`) and a
    line yields at most one turn, which is what
    :func:`session_transcript.entry_full_text` resolves against. Measured on
    a session launched through the launcher's own API for #1014 plus the
    264 conversations already on disk:

    * A ``PLANNER_RESPONSE`` carries ``thinking``, ``content`` and
      ``tool_calls`` in any combination — when the model calls a tool there
      is usually **no** ``content`` at all. All three come from one line at
      one offset, so nothing is merged across records and a reply can never
      be hoisted above the call that produced it (the trap #1012 hit).
    * **A tool result is the next ``MODEL`` step that is not a
      ``PLANNER_RESPONSE``, and its type does not name the tool** — a
      ``list_dir`` call answered as ``GENERIC``, not ``LIST_DIRECTORY``.
      Its text opens with the harness's ``Created At:`` / ``Completed At:``
      timing header, dropped here so the card shows the output itself.
      There is no call id anywhere, so calls and results pair positionally,
      FIFO. That held for 207 of the 210 logs on disk; the other three end
      on an unanswered call (which simply keeps ``result: None``), and six
      have a dropped step, whose orphan result stands alone rather than
      mis-pairing.
    * ``SYSTEM``-sourced steps are plumbing (ephemeral reminders, a
      compaction checkpoint, a tool-call parse error) and fold as
      ``system``; ``CONVERSATION_HISTORY`` carries no ``content`` and so
      produces nothing.
    * Timestamps are ISO strings already, in a mix of ``Z`` and local-offset
      forms within one file, both of which the client's ``new Date`` reads —
      no epoch normalisation is needed here, unlike Grok's (#1012).

    Known omission, shared with the Codex, Grok and Pi flavours (#1020): a
    failed tool result reads the same as a successful one. Antigravity gives
    a reader nothing structured to tell them apart — a command that exits 1
    is still ``status: "DONE"``, with the failure only in the result's
    English prose.

    ``uncapped`` — see :func:`src.transcript_flavors.claude.claude_entries`.
    On the fallback flat file ``truncated_fields`` names fields the harness
    itself already cut, and those entries report ``truncated`` however high
    the cap.
    """
    assistant_cap = NO_CAP if uncapped else _shared.ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else _shared.USER_TEXT_CAP
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    unanswered: List[str] = []
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None:
            continue
        step_type = str(obj.get("type") or "")
        cut = obj.get("truncated_fields")
        cut = set(cut) if isinstance(cut, list) else set()
        content = str(obj.get("content") or "")
        ts = obj.get("created_at")

        if step_type == "USER_INPUT":
            match = _AGY_REQUEST_RE.search(content)
            text = (match.group(1) if match else content).strip()
            if text:
                entries.append(_agy_mark(_text_entry(
                    "user", offset, ts, text, user_cap, sidechain=False), "content", cut))
            continue

        if str(obj.get("source") or "") == "SYSTEM":
            text = content.strip() or str(obj.get("error") or "").strip()
            if text:
                entries.append(_text_entry(
                    "system", offset, ts, text, SYSTEM_TEXT_CAP,
                    label=_AGY_SYSTEM_LABELS.get(step_type, "system"), sidechain=False,
                ))
            continue

        if step_type == "PLANNER_RESPONSE":
            thinking = str(obj.get("thinking") or "").strip()
            if thinking:
                entries.append(_agy_mark(_text_entry(
                    "thinking", offset, ts, thinking, THINKING_TEXT_CAP,
                    sidechain=False), "thinking", cut))
            if content.strip():
                entries.append(_agy_mark(_text_entry(
                    "assistant", offset, ts, content, assistant_cap,
                    sidechain=False), "content", cut))
            calls = obj.get("tool_calls")
            for index, call in enumerate(calls if isinstance(calls, list) else []):
                if not isinstance(call, dict):
                    continue
                key = f"{offset}:{index}"
                args = call.get("args")
                entry = _with_action(_entry(
                    "tool_call", offset, ts,
                    name=str(_agy_arg(call.get("name")) or "tool"),
                    summary=_agy_tool_summary(args),
                    result=None, result_truncated=False, sidechain=False,
                ), _tool_action(
                    str(_agy_arg(call.get("name")) or ""),
                    {k: _agy_arg(v) for k, v in args.items()} if isinstance(args, dict) else args,
                ))
                entries.append(entry)
                open_calls[key] = entry
                unanswered.append(key)
            continue

        # Every remaining MODEL step is a tool result, for the oldest call
        # still waiting on one. `status: "ERROR"` is Antigravity's only
        # structured failure signal and it covers *some* failures, not all
        # (#1020): a `view_file` on a missing path writes it (measured), a
        # shell command exiting 1 writes `status: "DONE"` with the failure
        # only in English prose ("The command exited with code 1."). So the
        # flag is set from the status and the flavour stays `partial` —
        # rather than reading the prose, which no reader here does.
        _attach_result(entries, open_calls, unanswered.pop(0) if unanswered else None,
                       _AGY_RESULT_HEADER_RE.sub("", content), offset, ts, False,
                       error=str(obj.get("status") or "") == "ERROR")
    return entries


def _antigravity_line_key(raw: str) -> Optional[str]:
    """Antigravity writes one self-contained step per line (see
    :func:`antigravity_entries`), so no line ever needs another."""
    return None
