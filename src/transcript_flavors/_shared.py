"""Generic parsing primitives shared by two or more harness flavours.

Split out of ``session_transcript.py`` (#1309) alongside the per-flavour
modules in this package. Everything here is either used by two or more
flavour builders (``claude.py``, ``codex.py``, ``grok.py``, ``pi.py``,
``antigravity.py``, ``copilot.py``) or by the paging/image code that stays
in ``session_transcript.py`` itself — nothing flavour-specific lives here,
so no flavour module needs to import another flavour module's helpers.
"""

from __future__ import annotations

import difflib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

Line = Tuple[int, str]
Entry = Dict[str, Any]
EntryBuilder = Callable[[List[Line]], List[Entry]]

# Display caps — the terminal stays the escape hatch for anything longer.
ASSISTANT_TEXT_CAP = 12_000
USER_TEXT_CAP = 4_000
THINKING_TEXT_CAP = 2_000
TOOL_SUMMARY_CAP = 200
TOOL_RESULT_CAP = 1_500
SYSTEM_TEXT_CAP = 1_500

# Effectively "no cap" for `entry_full_text` (#985): a turn's raw text never
# approaches this, so `_cap` never reports it truncated.
NO_CAP = 1 << 30


def _cap(text: str, cap: int) -> Tuple[str, bool]:
    text = text or ""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _loads(raw: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_summary(inputs: Any) -> str:
    """One line describing a tool call's input: the first string-valued
    argument (a command, a path, a prompt), else a compact key list."""
    if isinstance(inputs, str):
        text = inputs
    elif isinstance(inputs, dict):
        text = ""
        for value in inputs.values():
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            text = ", ".join(str(k) for k in inputs.keys())
    else:
        text = ""
    text = " ".join(text.split())
    return _cap(text, TOOL_SUMMARY_CAP)[0]


# What a tool call did, in plain words for Chat's runs (#1266). The tool names
# and argument keys were probed from real transcripts of every harness (key
# names only); a tool not listed, or an input missing its key, gets no action
# and renders exactly as before.
_COMMAND_TOOLS = {
    "Bash": "command", "PowerShell": "command",      # Claude
    "bash": "command", "powershell": "command",      # Pi, Copilot
    "run_terminal_command": "command",               # Grok
    "run_command": "CommandLine",                    # Antigravity
}
_RAW_COMMAND_TOOLS = frozenset({"exec"})             # Codex custom tool: the input is the command
_READ_TOOLS = {
    "Read": "file_path", "read": "path", "read_file": "target_file",
    "view_file": "AbsolutePath", "view": "path",
}
_WRITE_TOOLS = {
    "Write": ("file_path", "content"), "write": ("path", "content"),
    "write_to_file": ("TargetFile", "CodeContent"),
}
_EDIT_TOOLS = {
    "Edit": ("file_path", "old_string", "new_string"),
    "edit": ("path", "oldText", "newText"),           # Pi: one pair, or a list under `edits`
}
ACTION_COMMAND_CAP = 2_000


def _line_delta(old: str, new: str) -> Tuple[int, int]:
    """``(added, removed)`` lines between an edit's own old and new text."""
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _tool_action(name: str, inputs: Any, *, raw_text: bool = False) -> Optional[Dict[str, Any]]:
    """``{"verb": "ran" | "edited" | "wrote" | "read", ...}`` for a known tool, else None.

    ``raw_text`` marks an input that is the command itself (Codex's ``exec``
    custom tool), never a JSON argument string.
    """
    if raw_text:
        command = inputs.strip() if name in _RAW_COMMAND_TOOLS and isinstance(inputs, str) else ""
        return {"verb": "ran", "command": _cap(command, ACTION_COMMAND_CAP)[0]} if command else None
    if not isinstance(inputs, dict):
        return None

    def text(key: str) -> Optional[str]:
        value = inputs.get(key)
        return value if isinstance(value, str) and value.strip() else None

    if name in _COMMAND_TOOLS:
        command = text(_COMMAND_TOOLS[name])
        return {"verb": "ran", "command": _cap(command.strip(), ACTION_COMMAND_CAP)[0]} if command else None
    if name in _READ_TOOLS:
        path = text(_READ_TOOLS[name])
        return {"verb": "read", "path": path} if path else None
    if name in _WRITE_TOOLS:
        path_key, body_key = _WRITE_TOOLS[name]
        path, body = text(path_key), inputs.get(body_key)
        if not path or not isinstance(body, str):
            return None
        return {"verb": "wrote", "path": path, "added": len(body.splitlines()), "removed": 0}
    if name in _EDIT_TOOLS:
        path_key, old_key, new_key = _EDIT_TOOLS[name]
        path = text(path_key)
        edits = [inputs] if old_key in inputs else [e for e in inputs.get("edits") or [] if isinstance(e, dict)]
        pairs = [(e.get(old_key), e.get(new_key)) for e in edits]
        pairs = [(o, n) for o, n in pairs if isinstance(o, str) and isinstance(n, str)]
        if not path or not pairs:
            return None
        added = removed = 0
        for old, new in pairs:
            a, r = _line_delta(old, new)
            added, removed = added + a, removed + r
        return {"verb": "edited", "path": path, "added": added, "removed": removed}
    return None


def _with_action(entry: Entry, action: Optional[Dict[str, Any]]) -> Entry:
    """Attach :func:`_tool_action`'s result to a ``tool_call`` entry, when there is one."""
    if action:
        entry["action"] = action
    return entry


# ------------------------------------------------------ transcript images (#1265)
#
# An image block rides the transcript as base64. Only the flavour-agnostic
# extraction lives here; the byte cap, media-type sniff and the on-demand
# read/decode route (``transcript_image``) stay in ``session_transcript.py``
# since they serve the paging/image API, not a flavour builder.


def _line_image_blocks(content: Any) -> List[Dict[str, Any]]:
    """Every image block of one line's content list, in walk order."""
    blocks: List[Dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            blocks.append(block)
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            blocks.extend(b for b in block["content"] if isinstance(b, dict) and b.get("type") == "image")
    return blocks


def _image_data(block: Dict[str, Any]) -> Optional[str]:
    """The base64 payload of an image block (Claude ``source.data``, Pi ``data``)."""
    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data") if source.get("type") == "base64" else None
    else:
        data = block.get("data")
    return data if isinstance(data, str) and data else None


def _image_refs(blocks: List[Dict[str, Any]], offset: int, start: int) -> Tuple[List[Dict[str, int]], int]:
    """``(refs, placeholders)`` for image ``blocks`` numbered from ``start``."""
    refs: List[Dict[str, int]] = []
    missing = 0
    for n, block in enumerate(blocks, start):
        if _image_data(block):
            refs.append({"offset": offset, "n": n})
        else:
            missing += 1
    return refs, missing


def _with_placeholders(text: str, missing: int) -> str:
    return (text + "\n\n" if text else "") + " ".join(["[image]"] * missing) if missing else text


def _blocks_text(content: Any, *, types: Tuple[str, ...] = ("text",), key: str = "text") -> str:
    """Join the ``text`` of the content blocks whose type is in ``types``;
    a plain string is returned as-is."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in types:
            text = str(block.get(key) or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _entry(kind: str, offset: int, timestamp: Any, **fields: Any) -> Entry:
    e: Entry = {"kind": kind, "offset": offset, "timestamp": timestamp}
    e.update(fields)
    return e


def _text_entry(kind: str, offset: int, timestamp: Any, text: str, cap: int, **fields: Any) -> Entry:
    body, truncated = _cap(text.strip(), cap)
    return _entry(kind, offset, timestamp, text=body, truncated=truncated, **fields)


def _attach_result(entries: List[Entry], calls: Dict[str, Entry], call_id: Any,
                   text: str, offset: int, timestamp: Any, sidechain: bool,
                   error: bool = False,
                   decision: Optional[Dict[str, Any]] = None,
                   images: Optional[List[Dict[str, int]]] = None) -> None:
    """Pair one tool result with its call (#1020 adds ``error``).

    ``error`` is written onto the entry **only when true** — the single
    place every flavour funnels through, so a harness that records a
    failure keeps it this far. Its absence deliberately means "not
    reported as failed", *not* "succeeded": three of six harnesses cannot
    tell a failed call from a working one at all for some or all of their
    tools, and folding that gap into the success state is the defect this
    fixes rather than a shortcut it may take. Which harness can say what is
    declared once, per flavour, in :data:`session_transcript.FLAVORS`'
    ``tool_errors`` — the client reads that to decide whether an unmarked
    call means "fine" or "nobody can tell".

    ``decision`` is what a decision card needs from the answer (#1149's
    ``answers``, #1151's ``plan_outcome``…), written onto the call — or onto
    the standalone result, so a card whose call is already on screen can
    still close.
    """
    body, truncated = _cap(text.strip(), TOOL_RESULT_CAP)
    call = calls.pop(str(call_id), None) if call_id else None
    if call is not None and call.get("result") is None:
        call["result"] = body
        call["result_truncated"] = truncated
        if error:
            call["error"] = True
        if images:
            call["result_images"] = images
        call.update(decision or {})
        return
    # A result whose call fell off this page (or an unknown id): stands alone
    # so it is neither lost nor mis-paired.
    fields: Dict[str, Any] = {"error": True} if error else {}
    if images:
        fields["images"] = images
    fields.update(decision or {})
    entries.append(_entry(
        "tool_result", offset, timestamp, text=body, truncated=truncated,
        tool_use_id=str(call_id or ""), sidechain=sidechain, **fields,
    ))


def _harness_label(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("<system-reminder"):
        return "system-reminder"
    if stripped.startswith("<task-notification"):
        return "task-notification"
    if stripped.startswith("<command-") or stripped.startswith("<local-command-"):
        return "command"
    return "system"
