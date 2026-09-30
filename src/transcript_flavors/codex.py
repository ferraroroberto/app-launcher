"""Codex rollout ``response_item`` line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.transcript_flavors import _shared
from src.transcript_flavors._shared import (
    Entry,
    Hunk,
    Line,
    NO_CAP,
    SYSTEM_TEXT_CAP,
    THINKING_TEXT_CAP,
    _added_hunks,
    _attach_result,
    _blocks_text,
    _entry,
    _loads,
    _text_entry,
    _tool_action,
    _tool_summary,
    _with_action,
    _with_diff,
    diff_counts,
)

# Codex user-role messages that are harness plumbing, not a typed prompt —
# the Codex counterpart of `board_transcript._SKIP_USER_PREFIXES`.
_CODEX_SKIP_USER_PREFIXES = (
    "<environment_context>", "<user_instructions>", "<recommended_plugins>",
    "<skills_instructions>", "<turn_aborted>", "<permissions_instructions>",
    "<collaboration_mode>",
)


# ------------------------------------------------------- apply_patch (#1356)
#
# Codex edits files through one tool, ``apply_patch``, whose input is a patch
# text (``*** Begin Patch`` .. ``*** End Patch``) naming each file in an
# ``*** Add File:`` / ``*** Update File:`` / ``*** Delete File:`` section. It
# rides the rollout two ways, both probed from real rollouts: a direct
# ``custom_tool_call`` named ``apply_patch`` (2026-06/07) and, in every recent
# rollout, a JS ``exec`` call whose source holds the patch as a string literal
# handed to ``tools.apply_patch``. The patch carries no line numbers, so an
# update's diff is unnumbered; an Add File starts at line 1.
_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_PATCH_FAILED = "apply_patch verification failed"
_ADD, _UPDATE, _DELETE, _MOVE = "*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "


def parse_apply_patch(text: str) -> List[Dict[str, Any]]:
    """The file sections of one patch, in order, each as an action dict
    (``wrote`` for an Add, ``edited`` for an Update, ``deleted`` for a
    Delete; an Update that moves the file edits the new path and deletes the
    old). A text that is not a complete Begin..End patch reads as nothing."""
    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if line.strip() == _PATCH_BEGIN), None)
    if begin is None or not any(line.strip() == _PATCH_END for line in lines[begin:]):
        return []
    sections: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    hunk: Optional[Hunk] = None
    for line in lines[begin + 1:]:
        if line.strip() == _PATCH_END:
            break
        header = next((h for h in (_ADD, _UPDATE, _DELETE) if line.startswith(h)), None)
        if header:
            cur = {"op": header, "path": line[len(header):].strip(), "lines": [], "hunks": [], "move": None}
            sections.append(cur)
            hunk = None
        elif cur is None or line.startswith("*** End of File"):
            continue
        elif line.startswith(_MOVE) and cur["op"] == _UPDATE:
            cur["move"] = line[len(_MOVE):].strip()
        elif cur["op"] == _ADD:
            if line.startswith("+"):
                cur["lines"].append(line[1:])
        elif cur["op"] == _UPDATE:
            if line.startswith("@@"):
                hunk = {"old_start": None, "new_start": None, "lines": []}
                cur["hunks"].append(hunk)
            elif not line or line[0] in "+- ":
                if hunk is None:
                    hunk = {"old_start": None, "new_start": None, "lines": []}
                    cur["hunks"].append(hunk)
                hunk["lines"].append(line or " ")
    actions: List[Dict[str, Any]] = []
    for sec in sections:
        path = sec["path"]
        if not path:
            continue
        if sec["op"] == _DELETE:
            actions.append({"verb": "deleted", "path": path})
        elif sec["op"] == _ADD:
            hunks = _added_hunks("\n".join(sec["lines"]), numbered=True)
            actions.append(_with_diff(
                {"verb": "wrote", "path": path, "added": len(sec["lines"]), "removed": 0, "created": True},
                hunks,
            ))
        else:
            hunks = [h for h in sec["hunks"] if h["lines"]]
            added, removed = diff_counts(hunks)
            target = sec["move"] or path
            actions.append(_with_diff({"verb": "edited", "path": target, "added": added, "removed": removed}, hunks))
            if sec["move"] and sec["move"] != path:
                actions.append({"verb": "deleted", "path": path})
    return actions


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_JS_ESCAPE_RE = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|\n|.)", re.S)


def _js_unescape(body: str) -> str:
    """The value of a cooked JS string literal's body (``"…"``, ``'…'`` or a
    template literal): the escapes a patch text can hold."""
    def one(m: "re.Match[str]") -> str:
        g = m.group(1)
        if g in ("\n", "\r\n"):
            return ""                       # a line continuation
        if g[0] == "u":
            return chr(int(g[2:-1] if g[1] == "{" else g[1:], 16))
        if g[0] == "x" and len(g) == 3:
            return chr(int(g[1:], 16))
        return _JS_ESCAPES.get(g, g)
    return _JS_ESCAPE_RE.sub(one, body)


def _patch_literals(source: str) -> List[str]:
    """The value of every JS string literal in ``source`` that holds a
    complete patch. Handles ``"…"``, ``'…'``, a template literal and
    ``String.raw`…` ``; a template literal with a ``${…}`` interpolation is
    skipped, since its value cannot be known from the text."""
    found: List[str] = []
    at = 0
    while True:
        i = source.find(_PATCH_BEGIN, at)
        if i < 0:
            return found
        at = i + len(_PATCH_BEGIN)
        # Back from the patch to its opening quote, over the newline (real or
        # escaped) a literal may open with.
        j = i
        while j > 0:
            if source[j - 1] in " \t\r\n":
                j -= 1
            elif source[j - 2:j] == "\\n":
                j -= 2
            else:
                break
        if j == 0 or source[j - 1] not in "\"'`":
            continue
        quote = source[j - 1]
        raw = quote == "`" and source[:j - 1].endswith("String.raw")
        end = -1
        k = j
        while k < len(source):
            c = source[k]
            if c == "\\" and not raw:
                k += 2
                continue
            if quote == "`" and c == "$" and source[k + 1:k + 2] == "{":
                break
            if c == quote:
                end = k
                break
            k += 1
        if end < 0:
            continue
        value = source[j:end] if raw else _js_unescape(source[j:end])
        if _PATCH_END in value:
            found.append(value)
            at = end


def _patch_actions(name: str, tool_input: Any) -> List[Dict[str, Any]]:
    """The file actions of the patch a custom tool call carries: a direct
    ``apply_patch`` input, or the patch literal inside an ``exec`` script that
    mentions ``apply_patch``."""
    if not isinstance(tool_input, str):
        return []
    if name == "apply_patch":
        return parse_apply_patch(tool_input)
    if name in _shared._RAW_COMMAND_TOOLS and "apply_patch" in tool_input:
        return [a for lit in _patch_literals(tool_input) for a in parse_apply_patch(lit)]
    return []


def codex_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from a Codex rollout's ``response_item`` lines.

    ``message`` payloads carry ``input_text`` (user/developer) or
    ``output_text`` (assistant) blocks — developer-role and harness-tagged
    user messages are ``system``. ``function_call`` / ``custom_tool_call``
    pair with their ``*_output`` by ``call_id``; ``reasoning`` summaries
    become ``thinking``; ``agent_message`` (sub-agent mailbox traffic) is a
    sidechain ``system`` entry. Everything else (token usage, events, world
    state) is dropped.

    ``uncapped`` — see :func:`src.transcript_flavors.claude.claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else _shared.ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else _shared.USER_TEXT_CAP
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    patch_steps: Dict[str, List[Entry]] = {}    # call id → the per-file steps of its patch (#1356)
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None or obj.get("type") != "response_item":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = obj.get("timestamp")
        pt = payload.get("type")
        if pt == "message":
            role = str(payload.get("role") or "")
            if role == "assistant":
                text = _blocks_text(payload.get("content"), types=("output_text",))
                if text.strip():
                    entries.append(_text_entry("assistant", offset, ts, text, assistant_cap, sidechain=False))
            elif role in ("user", "developer"):
                text = _blocks_text(payload.get("content"), types=("input_text",))
                stripped = text.strip()
                if not stripped:
                    continue
                if role == "developer" or any(stripped.startswith(p) for p in _CODEX_SKIP_USER_PREFIXES):
                    entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                               label="developer" if role == "developer" else "system",
                                               sidechain=False))
                else:
                    entries.append(_text_entry("user", offset, ts, stripped, user_cap, sidechain=False))
        elif pt in ("function_call", "custom_tool_call"):
            e = _with_action(_entry(
                "tool_call", offset, ts,
                name=str(payload.get("name") or "tool"),
                summary=_tool_summary(payload.get("arguments") if pt == "function_call" else payload.get("input")),
                result=None, result_truncated=False, sidechain=False,
            ), _tool_action(str(payload.get("name") or ""), payload.get("input"), raw_text=True)
                if pt == "custom_tool_call" else None)
            entries.append(e)
            call_id = str(payload.get("call_id") or "")
            if call_id:
                open_calls[call_id] = e
            # A patch's file edits: one step each, after the call's own row,
            # sharing its result (#1356).
            siblings = [
                _with_action(_entry(
                    "tool_call", offset, ts, name="apply_patch", summary=str(a["path"]),
                    result=None, result_truncated=False, sidechain=False,
                ), a)
                for a in (_patch_actions(str(payload.get("name") or ""), payload.get("input"))
                          if pt == "custom_tool_call" else [])
            ]
            entries.extend(siblings)
            if siblings and call_id:
                patch_steps[call_id] = siblings
        elif pt in ("function_call_output", "custom_tool_call_output"):
            # No structural `error=` here, deliberately (#1020): Codex records
            # **no** success/error field at any level. A failed call and a
            # working one are structurally identical — measured over the 30
            # newest rollouts (one key-set, `call_id,id,
            # internal_chat_message_metadata_passthrough,output,type`) and
            # re-confirmed by probe, where a failing tool wrote "Script
            # failed" and a working one "Script completed" in the same
            # `output` text and nothing else differed. Telling them apart
            # would mean reading that prose, so the flavour is declared
            # `partial` in `FLAVORS`: the client says "can't tell" for an
            # unmarked call rather than showing a failure as a success.
            # The one exception (#1356) is a patch: `apply_patch` states its
            # own rejection in a fixed tool message (28 of 710 direct calls
            # and 79 of ~1,100 `exec` ones on the dev box), and a rejected
            # patch changed nothing, so its steps must not count as edits.
            output = payload.get("output")
            text = output if isinstance(output, str) else _blocks_text(output, types=("input_text", "output_text", "text"))
            call_id = str(payload.get("call_id") or "")
            steps = patch_steps.pop(call_id, [])
            failed = bool(steps) and _PATCH_FAILED in text
            _attach_result(entries, open_calls, call_id, text, offset, ts, False, error=failed)
            for step in steps:
                step["result"], step["result_truncated"] = _shared._cap(text.strip(), _shared.TOOL_RESULT_CAP)
                if failed:
                    step["error"] = True
                    step["action"].pop("diff", None)
        elif pt == "reasoning":
            text = _blocks_text(payload.get("summary"), types=("summary_text",))
            if text.strip():
                entries.append(_text_entry("thinking", offset, ts, text, THINKING_TEXT_CAP, sidechain=False))
        elif pt == "agent_message":
            text = _blocks_text(payload.get("content"), types=("input_text", "output_text"))
            if text.strip():
                entries.append(_text_entry("system", offset, ts, text, SYSTEM_TEXT_CAP,
                                           label="sub-agent", sidechain=True))
    return entries


def _codex_line_key(raw: str) -> Optional[str]:
    """Codex rollout records are self-contained: never grouped."""
    return None
