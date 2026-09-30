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
# Each argument is a tuple of key names, the first one present wins: the same
# tool name carries different keys per harness (Pi's ``write`` takes ``path``,
# Grok's ``file_path``). Every key was read from a real call or the harness's
# own tool schema (#1356), never guessed.
_WRITE_TOOLS = {
    "Write": (("file_path",), ("content",)),
    "write": (("path", "file_path"), ("content",)),    # Pi / Grok
    "write_to_file": (("TargetFile",), ("CodeContent",)),
    "create": (("path",), ("file_text",)),             # Copilot
}
_EDIT_TOOLS = {
    "Edit": (("file_path",), ("old_string",), ("new_string",)),
    "MultiEdit": (("file_path",), ("old_string",), ("new_string",)),   # Claude: a list under `edits`
    "edit": (("path",), ("oldText", "old_str"), ("newText", "new_str")),   # Pi (one pair, or a list under `edits`) / Copilot
    "search_replace": (("file_path",), ("old_string",), ("new_string",)),  # Grok
}
ACTION_COMMAND_CAP = 2_000

# Per-step diffs (#1349). An edit/write action carries ``diff``: ``hunks``,
# each ``{"old_start", "new_start", "lines"}`` with every line keeping its
# unified-diff prefix (" ", "-", "+"), plus ``numbered`` and ``truncated``.
# The starts are real file line numbers only where the harness recorded them
# (Claude's ``structuredPatch``, or a Write that created the file); a diff
# worked out from an edit's own old/new text has none, so they are ``None`` --
# never invented. A builder caps at DIFF_FULL_BYTES (the #977 viewer's cap);
# a transcript page trims each step further, see
# ``session_transcript._inline_diffs``.
DIFF_FULL_BYTES = 200_000
DIFF_CONTEXT = 3

Hunk = Dict[str, Any]


def _edit_hunks(old: str, new: str) -> Tuple[List[Hunk], int, int]:
    """``(hunks, added, removed)`` between an edit's own old and new text --
    one matcher for both, so the counts are exactly the diff's +/- lines."""
    a, b = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    hunks: List[Hunk] = []
    for group in matcher.get_grouped_opcodes(DIFF_CONTEXT):
        lines: List[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend(" " + line for line in a[i1:i2])
                continue
            if tag in ("replace", "delete"):
                lines.extend("-" + line for line in a[i1:i2])
            if tag in ("replace", "insert"):
                lines.extend("+" + line for line in b[j1:j2])
        hunks.append({"old_start": None, "new_start": None, "lines": lines})
    return hunks, added, removed


def _added_hunks(text: str, *, numbered: bool = False) -> List[Hunk]:
    """A whole new text as one all-added hunk (a Write). ``numbered`` only
    when the file is known to be new, so line 1 really is line 1."""
    lines = ["+" + line for line in text.splitlines()]
    if not lines:
        return []
    return [{
        "old_start": 0 if numbered else None,
        "new_start": 1 if numbered else None,
        "lines": lines,
    }]


def _patch_hunks(patch: Any) -> Optional[List[Hunk]]:
    """Claude's ``toolUseResult.structuredPatch`` as hunks, or None when it
    is absent, empty or not the probed shape (``oldStart``/``newStart`` ints,
    ``lines`` strings each carrying its own prefix)."""
    if not isinstance(patch, list) or not patch:
        return None
    hunks: List[Hunk] = []
    for h in patch:
        if not isinstance(h, dict):
            return None
        old_start, new_start, lines = h.get("oldStart"), h.get("newStart"), h.get("lines")
        if not isinstance(old_start, int) or not isinstance(new_start, int) or not isinstance(lines, list):
            return None
        if not all(isinstance(line, str) for line in lines):
            return None
        hunks.append({"old_start": old_start, "new_start": new_start, "lines": list(lines)})
    return hunks


def diff_counts(hunks: List[Hunk]) -> Tuple[int, int]:
    """``(added, removed)`` lines of a diff."""
    added = removed = 0
    for h in hunks:
        for line in h["lines"]:
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return added, removed


def cap_diff(hunks: List[Hunk], max_bytes: int, max_lines: Optional[int] = None) -> Dict[str, Any]:
    """``{"hunks", "numbered", "truncated"}`` holding at most ``max_bytes``
    (and ``max_lines``) of diff lines; a hunk cut short keeps what fits."""
    kept: List[Hunk] = []
    used = count = 0
    truncated = False
    for h in hunks:
        lines: List[str] = []
        for line in h["lines"]:
            cost = len(line.encode("utf-8", errors="replace")) + 1
            if used + cost > max_bytes or (max_lines is not None and count >= max_lines):
                truncated = True
                break
            lines.append(line)
            used += cost
            count += 1
        if lines:
            kept.append({**h, "lines": lines})
        if truncated:
            break
    return {
        "hunks": kept,
        "numbered": bool(hunks) and all(h["new_start"] is not None for h in hunks),
        "truncated": truncated,
    }


def _tool_action(name: str, inputs: Any, *, raw_text: bool = False) -> Optional[Dict[str, Any]]:
    """``{"verb": "ran" | "edited" | "wrote" | "read", ...}`` for a known tool, else None.
    (A Codex ``apply_patch`` step can also be ``"deleted"``: built in
    ``codex.py``, not here, since it has no tool-input shape of its own.)

    ``raw_text`` marks an input that is the command itself (Codex's ``exec``
    custom tool), never a JSON argument string.
    """
    if raw_text:
        command = inputs.strip() if name in _RAW_COMMAND_TOOLS and isinstance(inputs, str) else ""
        return {"verb": "ran", "command": _cap(command, ACTION_COMMAND_CAP)[0]} if command else None
    if not isinstance(inputs, dict):
        return None

    def first(keys: Tuple[str, ...], source: Dict[str, Any]) -> Optional[str]:
        return next((k for k in keys if k in source), None)

    def text(keys: Tuple[str, ...]) -> Optional[str]:
        for key in keys:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    if name in _COMMAND_TOOLS:
        command = text((_COMMAND_TOOLS[name],))
        return {"verb": "ran", "command": _cap(command.strip(), ACTION_COMMAND_CAP)[0]} if command else None
    if name in _READ_TOOLS:
        path = text((_READ_TOOLS[name],))
        return {"verb": "read", "path": path} if path else None
    if name in _WRITE_TOOLS:
        path_keys, body_keys = _WRITE_TOOLS[name]
        path, body_key = text(path_keys), first(body_keys, inputs)
        body = inputs.get(body_key) if body_key else None
        if not path or not isinstance(body, str):
            return None
        return _with_diff(
            {"verb": "wrote", "path": path, "added": len(body.splitlines()), "removed": 0},
            _added_hunks(body),
        )
    if name in _EDIT_TOOLS:
        path_keys, old_keys, new_keys = _EDIT_TOOLS[name]
        path = text(path_keys)
        edits = [inputs] if first(old_keys, inputs) else [e for e in inputs.get("edits") or [] if isinstance(e, dict)]
        pairs = []
        for e in edits:
            old_key, new_key = first(old_keys, e), first(new_keys, e)
            pairs.append((e.get(old_key) if old_key else None, e.get(new_key) if new_key else None))
        pairs = [(o, n) for o, n in pairs if isinstance(o, str) and isinstance(n, str)]
        if not path or not pairs:
            return None
        # A multi-edit (Claude's MultiEdit, Pi's `edits`) is one combined
        # diff: each pair's hunks in the order the edits were applied.
        hunks: List[Hunk] = []
        added = removed = 0
        for old, new in pairs:
            h, a, r = _edit_hunks(old, new)
            hunks.extend(h)
            added, removed = added + a, removed + r
        return _with_diff({"verb": "edited", "path": path, "added": added, "removed": removed}, hunks)
    return None


def _with_diff(action: Dict[str, Any], hunks: List[Hunk]) -> Dict[str, Any]:
    """Attach a capped ``diff`` to an edit/write action; an edit that
    changes no line keeps today's action with no diff at all."""
    if hunks:
        action["diff"] = cap_diff(hunks, DIFF_FULL_BYTES)
    return action


def recorded_hunks(patch: Any, created_text: Optional[str] = None) -> List[Hunk]:
    """The diff a harness recorded for one edit: ``patch`` in Claude's
    ``structuredPatch`` shape, else ``created_text`` as a new file from line
    1, else nothing. The one reading both a Chat step (#1349) and the
    session's Changed files fold count from, so their totals agree."""
    hunks = _patch_hunks(patch)
    if hunks is None and isinstance(created_text, str):
        hunks = _added_hunks(created_text, numbered=True)
    return hunks or []



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
            # A failed edit changed nothing (#1349): its row keeps today's
            # shape, never a diff of what it only attempted.
            action = call.get("action")
            if isinstance(action, dict):
                action.pop("diff", None)
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
