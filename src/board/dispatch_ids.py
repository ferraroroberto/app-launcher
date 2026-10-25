"""Background-dispatch ids read off one transcript line, with no file IO.

Each helper takes one parsed JSONL object and returns the ids it launches or
reports finished: a backgrounded sub-agent or shell, or a ``Monitor`` watch.
:mod:`src.board.transcript` pairs launches with completions across a tail.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# Pending-background-dispatch detection: the id a completed dispatch
# is later referenced by, e.g. "<task-id>btvos2agp</task-id>".
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")


def _launched_background_ids(obj: Dict[str, Any]) -> List[str]:
    """Background-dispatch ids a transcript line's tool result evidences.

    The synchronous ack for a backgrounded dispatch rides ``toolUseResult``
    (a sibling of ``message``, not inside it, per live transcripts): a
    backgrounded ``Bash`` command carries ``backgroundTaskId``; an async
    sub-agent dispatch (the ``Agent``/``Task`` tool) carries ``isAsync: true``
    plus ``agentId``. Both ids later reappear inside a completion's
    ``<task-id>`` (see :func:`_notified_background_ids`).
    """
    result = obj.get("toolUseResult")
    if not isinstance(result, dict):
        return []
    ids: List[str] = []
    background_id = result.get("backgroundTaskId")
    if isinstance(background_id, str) and background_id:
        ids.append(background_id)
    if result.get("isAsync"):
        agent_id = result.get("agentId")
        if isinstance(agent_id, str) and agent_id:
            ids.append(agent_id)
    return ids


def _notification_text(obj: Dict[str, Any]) -> str:
    """The ``<task-notification>`` payload of a line, or ``""``."""
    text = obj.get("content")
    if not isinstance(text, str) or "<task-notification>" not in text:
        attachment = obj.get("attachment")
        text = attachment.get("prompt") if isinstance(attachment, dict) else None
    return text if isinstance(text, str) and "<task-notification>" in text else ""


def _notified_background_ids(obj: Dict[str, Any]) -> List[str]:
    """Background-dispatch ids a ``<task-notification>`` line marks complete.

    Claude Code injects the completion notice as a ``queue-operation`` line's
    plain ``content`` string, or an ``attachment`` line's
    ``attachment.prompt`` — never as an ordinary ``assistant``/``user``
    message, so it is invisible to :func:`_last_activity` (empirically
    confirmed against live transcripts, app-launcher#464). Either shape
    carries the same ``<task-id>ID</task-id>``.
    """
    return _TASK_ID_RE.findall(_notification_text(obj))


_BACKGROUNDABLE_TOOL_NAMES = frozenset({"Bash", "PowerShell"})


def _is_background_dispatch_tool_use(block: Any) -> bool:
    """Whether an assistant ``tool_use`` content block is a ``Bash`` or
    ``PowerShell`` call dispatched with ``run_in_background: true``.

    Read directly off the ``tool_use`` block itself — the Anthropic
    message-content-block shape (every tool call carries ``type``, ``name``,
    ``id``, ``input``) is part of the stable message format, unlike the
    internal ``toolUseResult.backgroundTaskId`` key
    :func:`_launched_background_ids` depends on, which a live-transcript
    spot-check for #576 found had silently stopped appearing: a fresh
    ``run_in_background`` Bash dispatch and its eventual
    ``<task-notification>`` both showed up in the tail, but no
    ``backgroundTaskId`` anywhere — the old check sees nothing launched and
    never overrides the status. ``PowerShell`` shares the same
    ``run_in_background`` input shape and is this repo's own agents' actual
    backgrounding tool on Windows (#594) — restricting the check to ``Bash``
    left it blind to exactly that case.
    """
    return (
        isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") in _BACKGROUNDABLE_TOOL_NAMES
        and isinstance(block.get("input"), dict)
        and block["input"].get("run_in_background") is True
    )


def _launched_bash_dispatch_ids(obj: Dict[str, Any]) -> List[str]:
    """``tool_use`` ids of backgrounded ``Bash``/``PowerShell`` dispatches an
    assistant line launches (#576, widened to ``PowerShell`` by #594) —
    correlated against the tool call's own ``id``, not the internal
    ``backgroundTaskId`` :func:`_launched_background_ids` reads out of the
    result.
    """
    if obj.get("type") != "assistant":
        return []
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    return [
        block["id"]
        for block in content
        if _is_background_dispatch_tool_use(block) and isinstance(block.get("id"), str)
    ]


# A completion notification's own correlation tag — carried alongside
# ``<task-id>`` in the real ``<task-notification>`` payload (confirmed
# against a live transcript), so it resolves a dispatch even when nothing
# ever surfaced the internal ``backgroundTaskId`` the legacy check keys on.
# Deliberately *not* matched against an ordinary ``tool_result`` for the same
# ``tool_use_id``: a backgrounded Bash call's synchronous reply is just the
# "launched" ack, not real completion — treating it as done would defeat the
# whole point of this check the instant the dispatch fires.
_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")


def _notified_bash_dispatch_ids(obj: Dict[str, Any]) -> List[str]:
    """``tool_use`` ids a ``<task-notification>`` line's ``<tool-use-id>``
    tag proves resolved (#576)."""
    return _TOOL_USE_ID_RE.findall(_notification_text(obj))


# A ``Monitor`` is a third kind of background work. Its launch ack carries
# ``toolUseResult.taskId``; the events it emits arrive as ``<task-notification>``
# lines under that same id but with no ``<status>`` (a bare event leaves the
# monitor running), while its end — stream ended, script failed, stopped — carries
# one. A monitor that outlives its ``timeout_ms`` ends with a final event instead.
_MONITOR_TIMEOUT_EVENT = "[Monitor timed out"
_STATUS_TAG = "<status>"


def _launched_monitor_use_ids(obj: Dict[str, Any]) -> List[str]:
    """``tool_use`` ids of the ``Monitor`` calls an assistant line makes."""
    if obj.get("type") != "assistant":
        return []
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    return [
        block["id"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == "Monitor"
        and isinstance(block.get("id"), str)
    ]


def _monitor_ack(obj: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """``(tool_use_id, taskId)`` when a ``user`` line is a ``Monitor`` launch ack."""
    result = obj.get("toolUseResult")
    task_id = result.get("taskId") if isinstance(result, dict) else None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(task_id, str) or not task_id or not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            use_id = block.get("tool_use_id")
            if isinstance(use_id, str):
                return use_id, task_id
    return None


def _monitor_notification(obj: Dict[str, Any]) -> Optional[Tuple[str, bool]]:
    """``(task_id, ended)`` for a ``Monitor`` notification, else ``None``.

    Told apart from a Bash or Agent completion by its summary, which always
    opens ``Monitor``. A bare event is alive; the terminal shapes and the
    timeout event are ended.
    """
    text = _notification_text(obj)
    ids = _TASK_ID_RE.findall(text)
    if not ids or "<summary>Monitor" not in text:
        return None
    if "<summary>Monitor event:" in text:
        return ids[0], _MONITOR_TIMEOUT_EVENT in text
    return ids[0], _STATUS_TAG in text
