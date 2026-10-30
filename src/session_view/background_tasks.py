"""A Claude session's background tasks (#1955), folded from its transcript.

The session's ⋮ → *Background tasks* lists what the agent started with
``run_in_background`` (Bash, PowerShell) or ``Monitor`` and has not heard
the end of yet (*running*), and what has ended (*finished*, with its status
and summary), the way Claude Code's own apps do. Read-only: Claude Code
exposes no way to stop a task from outside its session, so this module
only observes.

The transcript carries three facts per task, joined on the tool-use id and
the task id:

* the launching ``tool_use`` (name, description, command);
* its ``tool_result``, whose ``toolUseResult`` names the task
  (``backgroundTaskId`` for a shell command, ``taskId`` for a monitor);
* a ``<task-notification>`` Claude Code queues when the task ends, carrying
  ``<status>``. A monitor also sends one per event, with no status: those
  leave the task running.

A launch with no status notification is running; the first notification
with a status finishes it.

A task's output lives in a file under the OS temp folder,
``<temp>/claude/<project>/<session id>/tasks/<task id>.output``, whose
path the transcript names. The path is data from a file, so
:func:`output_path_for` accepts it only in exactly that shape, for this
session's id and this task's id, and never through a link; any other
path is refused rather than read. :func:`read_output_tail` returns at most
:data:`OUTPUT_TAIL_BYTES` of its end.

Bounded work per request: the fold reads at most :data:`SCAN_CEILING`
bytes, the newest part of the file, and skips the two line types that
repeat the whole system prompt.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

SCAN_CEILING = 64 * 1024 * 1024
# The newest finished tasks listed; the rest are counted, not sent.
FINISHED_LIMIT = 30
OUTPUT_TAIL_BYTES = 8 * 1024
LABEL_CHARS = 200
COMMAND_CHARS = 600

_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_STATUS = re.compile(r"^[a-z][a-z_-]{0,19}$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Lines that carry the whole system prompt and no task fact.
_SKIP_TYPES = (b"prompt_snapshot", b"api-request-shape")
_MARKERS = (b"run_in_background", b"Monitor", b"backgroundTaskId", b"taskId", b"task-notification")
_TEXT_ID = re.compile(r"(?:background with ID|moved to the background \(ID): (\w+)")
_TEXT_PATH = re.compile(r"Output is being written to: (.+?\.output)")


def _field(text: str, tag: str) -> Optional[str]:
    match = re.search(r"<%s>(.*?)</%s>" % (tag, tag), text, re.S)
    return match.group(1).strip() if match else None


def _lines(path: Path) -> Iterator[bytes]:
    """The newest :data:`SCAN_CEILING` bytes of ``path``, line by line,
    without the line a cut lands inside."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > SCAN_CEILING:
            handle.seek(size - SCAN_CEILING)
            handle.readline()
        for line in handle:
            if any(m in line for m in _MARKERS) and not any(s in line for s in _SKIP_TYPES):
                yield line


def _launch_blocks(row: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _notification_texts(row: Dict[str, Any]) -> Iterator[str]:
    """Every string on a line that can hold a ``<task-notification>``: the
    queue entry, the delivered attachment, or a user turn."""
    if row.get("type") == "queue-operation":
        if row.get("operation") == "enqueue" and isinstance(row.get("content"), str):
            yield row["content"]
        return
    attachment = row.get("attachment")
    if isinstance(attachment, dict) and isinstance(attachment.get("prompt"), str):
        yield attachment["prompt"]
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                yield block["text"]


def _label(inputs: Dict[str, Any]) -> Tuple[str, str]:
    command = str(inputs.get("command") or "")
    first_line = next((ln.strip() for ln in command.splitlines() if ln.strip()), "")
    label = str(inputs.get("description") or "").strip() or first_line or "Background task"
    return label[:LABEL_CHARS], command[:COMMAND_CHARS]


def _seconds_between(start: Optional[str], end: Optional[str]) -> Optional[int]:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds()))


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fold_tasks(path: Path) -> Dict[str, Dict[str, Any]]:
    """Every task the transcript mentions, by task id, in launch order:
    ``{id, kind, label, command, started_at, output_file, status,
    finished_at, summary}`` — ``status`` is ``None`` while it runs."""
    uses: Dict[str, Dict[str, Any]] = {}
    tasks: Dict[str, Dict[str, Any]] = {}
    last_ts: Optional[str] = None
    for raw in _lines(path):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ts = row.get("timestamp") if isinstance(row.get("timestamp"), str) else last_ts
        last_ts = ts
        for block in _launch_blocks(row):
            kind = block.get("type")
            inputs = block.get("input")
            if kind == "tool_use" and isinstance(inputs, dict):
                if inputs.get("run_in_background") or block.get("name") == "Monitor":
                    label, command = _label(inputs)
                    uses[str(block.get("id"))] = {
                        "kind": str(block.get("name") or "task").lower(),
                        "label": label, "command": command,
                    }
            elif kind == "tool_result" and str(block.get("tool_use_id")) in uses:
                _record_launch(tasks, uses[str(block["tool_use_id"])], block, row, ts)
        for text in _notification_texts(row):
            if "<task-notification>" in text:
                _record_notification(tasks, text, ts)
    return tasks


def _record_launch(
    tasks: Dict[str, Dict[str, Any]], use: Dict[str, Any], block: Dict[str, Any],
    row: Dict[str, Any], ts: Optional[str],
) -> None:
    result = row.get("toolUseResult")
    text = block.get("content")
    text = text if isinstance(text, str) else ""
    task_id = None
    if isinstance(result, dict):
        task_id = result.get("backgroundTaskId") or result.get("taskId")
    if not task_id:
        match = _TEXT_ID.search(text)
        task_id = match.group(1) if match else None
    if not task_id or not _TASK_ID.match(str(task_id)):
        return
    found = _TEXT_PATH.search(text)
    tasks.setdefault(str(task_id), {
        "id": str(task_id), **use, "started_at": ts,
        "output_file": found.group(1) if found else None,
        "status": None, "finished_at": None, "summary": "",
    })


def _record_notification(tasks: Dict[str, Dict[str, Any]], text: str, ts: Optional[str]) -> None:
    task_id = _field(text, "task-id")
    if not task_id or not _TASK_ID.match(task_id):
        return
    status = (_field(text, "status") or "").lower()
    summary = html.unescape(_field(text, "summary") or "")
    task = tasks.get(task_id)
    if task is None:
        # Launched before the part of the file that was read.
        task = tasks[task_id] = {
            "id": task_id, "kind": "task", "label": summary[:LABEL_CHARS] or "Background task",
            "command": "", "started_at": None, "output_file": None,
            "status": None, "finished_at": None, "summary": "",
        }
    if not task["output_file"]:
        task["output_file"] = _field(text, "output-file")
    if status and task["status"] is None:
        task["status"] = status if _STATUS.match(status) else "finished"
        task["finished_at"] = ts
        task["summary"] = summary[:LABEL_CHARS * 2]


def _public(
    task: Dict[str, Any], session_id: str, now: datetime, temp_root: Optional[Path],
) -> Dict[str, Any]:
    end = task["finished_at"] or now.isoformat()
    return {
        "id": task["id"], "kind": task["kind"], "label": task["label"],
        "command": task["command"],
        "status": task["status"] or "running",
        "started_at": task["started_at"], "finished_at": task["finished_at"],
        "elapsed_seconds": _seconds_between(task["started_at"], end),
        "summary": task["summary"],
        "has_output": output_path_for(task, session_id, temp_root) is not None,
    }


def background_tasks(
    path: Path, now: Optional[datetime] = None, temp_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """``{running, finished, finished_total}`` for the session whose
    transcript is ``path`` (its stem is the session id). Running tasks come
    newest first, then the newest :data:`FINISHED_LIMIT` finished ones."""
    now = now or datetime.now(timezone.utc)
    session_id = path.stem
    folded = [_public(t, session_id, now, temp_root) for t in fold_tasks(path).values()]
    running = [t for t in folded if t["status"] == "running"]
    finished = [t for t in folded if t["status"] != "running"]
    running.sort(key=lambda t: t["started_at"] or "", reverse=True)
    finished.sort(key=lambda t: t["finished_at"] or "", reverse=True)
    return {
        "running": running,
        "finished": finished[:FINISHED_LIMIT],
        "finished_total": len(finished),
    }


def output_path_for(
    task: Dict[str, Any], session_id: str, temp_root: Optional[Path] = None,
) -> Optional[Path]:
    """The task's output file, or ``None`` when the transcript names none
    or names a path other than this session's own
    ``<temp>/claude/<project>/<session id>/tasks/<task id>.output`` that
    exists as a plain file (a link of any kind is refused)."""
    raw = task.get("output_file")
    if not raw or not _TASK_ID.match(str(task.get("id"))) or not _TASK_ID.match(session_id):
        return None
    root = Path(os.path.realpath(temp_root or Path(tempfile.gettempdir()) / "claude"))
    candidate = Path(str(raw))
    try:
        resolved = Path(os.path.realpath(candidate))
        parts = resolved.relative_to(root).parts
    except (ValueError, OSError):
        return None
    if len(parts) != 4 or parts[1] != session_id or parts[2] != "tasks":
        return None
    if parts[3] != "%s.output" % task["id"]:
        return None
    # realpath followed any link in the middle, so the path it settled on
    # must be the one the transcript named, spelling aside.
    if os.path.normcase(str(resolved)) != os.path.normcase(os.path.realpath(os.path.abspath(candidate))):
        return None
    if os.path.islink(candidate) or not resolved.is_file():
        return None
    return resolved


def read_output_tail(file: Path) -> Dict[str, Any]:
    """``{text, size, truncated}``: the last :data:`OUTPUT_TAIL_BYTES` of the
    file, from the first whole line when it was cut, without terminal
    colour codes."""
    size = file.stat().st_size
    with file.open("rb") as handle:
        if size > OUTPUT_TAIL_BYTES:
            handle.seek(size - OUTPUT_TAIL_BYTES)
        data = handle.read(OUTPUT_TAIL_BYTES)
    truncated = size > OUTPUT_TAIL_BYTES
    if truncated and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    text = _ANSI.sub("", data.decode("utf-8", errors="replace")).replace("\r\n", "\n")
    return {"text": text, "size": size, "truncated": truncated}


def task_output(path: Path, task_id: str, temp_root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """One task's output tail, or ``None`` when the session has no such
    task or its output file is missing or not one of its own."""
    task = fold_tasks(path).get(task_id)
    if task is None:
        return None
    file = output_path_for(task, path.stem, temp_root)
    return read_output_tail(file) if file is not None else None
