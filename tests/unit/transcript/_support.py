"""JSONL builders and page walkers shared by the transcript unit modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src import session_transcript as st


# ------------------------------------------------------------ fixtures


def _write_jsonl(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def _user(text: Any, **extra: Any) -> Dict[str, Any]:
    row = {"type": "user", "timestamp": "2026-09-14T10:00:00Z",
           "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


def _assistant(blocks: List[Dict[str, Any]], msg_id: str = "m1", **extra: Any) -> Dict[str, Any]:
    row = {"type": "assistant", "timestamp": "2026-09-14T10:00:01Z",
           "message": {"id": msg_id, "role": "assistant", "content": blocks}}
    row.update(extra)
    return row


def _tool_use(name: str, inputs: Dict[str, Any], tid: str) -> Dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def _tool_result(tid: str, content: Any) -> Dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


def _lines_of(path: Path) -> List[Any]:
    """Every non-blank line of ``path`` as the ``(byte offset, text)`` pairs
    the entry builders take."""
    raw = path.read_bytes()
    lines, pos = [], 0
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            lines.append((pos, chunk.decode("utf-8", errors="replace")))
        pos += len(chunk) + 1
    return lines


def _parse_whole(
    path: Path, build=st.claude_entries, *, tail: int = -1
) -> List[Dict[str, Any]]:
    """Reference: one parse of the whole file — offset stripped from the
    folded kinds, kept on turns, matching :func:`session_transcript._page`
    (#985).

    ``tail`` is the live-tail offset of the page(s) being compared (#1050):
    from there on a folded entry keeps its offset too, because that region
    is what a live view re-renders and it needs a key per card. Default -1
    means "no page ended at EOF", i.e. strip them all.
    """
    entries = build(_lines_of(path))
    for e in entries:
        if not st._is_turn(e) and not (tail >= 0 and e.get("offset", -1) >= tail):
            e.pop("offset", None)
    return entries


def _tail_of(path: Path, limit: int, flavor: str = "claude") -> int:
    """The newest page's live-tail offset (#1050) — where its provisional
    region begins, and so which entries keep a folded offset."""
    return st.transcript_page(path, limit=limit, flavor=flavor)["tail"]


def _walk(path: Path, limit: int, flavor: str = "claude") -> List[List[Dict[str, Any]]]:
    """Every page from newest to oldest, following ``next_cursor``."""
    pages, cursor, guard = [], None, 0
    while True:
        page = st.transcript_page(path, before=cursor, limit=limit, flavor=flavor)
        pages.append(page["entries"])
        cursor = page["next_cursor"]
        guard += 1
        assert guard < 10_000, "cursor never reached the file start"
        if cursor is None:
            return pages


def _kinds(entries: List[Dict[str, Any]]) -> List[str]:
    return [e["kind"] for e in entries]


# ------------------------------------------------------- transcript_page


def _conversation(turns: int, *, tool_calls_per_turn: int = 2) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for i in range(turns):
        lines.append(_user(f"prompt {i} " + "p" * (i % 7)))
        mid = f"m{i}"
        lines.append(_assistant([{"type": "thinking", "thinking": f"think {i}"}], mid))
        lines.append(_assistant([{"type": "text", "text": f"reply {i} opener"}], mid))
        for k in range(tool_calls_per_turn):
            tid = f"t{i}-{k}"
            lines.append(_assistant([_tool_use("Bash", {"command": f"cmd {i} {k}"}, tid)], mid))
            lines.append(_user([_tool_result(tid, "out " + "o" * ((i * k) % 50))]))
        lines.append(_assistant([{"type": "text", "text": f"reply {i} closer " + "r" * (i % 11)}], mid))
    return lines


# --------------------------------------------------------- codex_entries


def _codex(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "response_item", "timestamp": "2026-09-08T15:49:10Z", "payload": payload}


# The captured Grok Build session (#1012): two probe prompts were text-only,
# so this one was recorded specifically to pin the two shapes they missed —
# a read-only tool call with its result, and a long multi-paragraph reply.
# Redacted (home paths as `~`), never hand-written.
GROK_CAPTURE = Path(__file__).resolve().parents[2] / "fixtures" / "grok_updates.jsonl"


def _grok(update: Dict[str, Any], *, ts: int = 1_789_670_620, **meta: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "timestamp": ts, "method": "session/update",
        "params": {"sessionId": "s-1", "update": update, "_meta": dict(meta)},
    }
    return row


def _grok_chunk(kind: str, text: str, **kw: Any) -> Dict[str, Any]:
    return _grok({"sessionUpdate": kind, "content": {"type": "text", "text": text}}, **kw)


def _png_b64() -> str:
    """A real 2x2 PNG, generated: synthetic image bytes only (#1265)."""
    import base64
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


# An Antigravity CLI session captured for #1014 by launching it through the
# launcher's own API from this repo's folder and driving it over `/input`,
# so the correlation below is the real one. Two turns — a read-only
# `list_dir` and a `run_command` made to fail on purpose — plus the
# `quit`/`Goodbye!` pair the launcher's Stop leaks into the history (#1016),
# left in on purpose: the parser must not special-case it.
# Redacted: the working directory becomes `E:\work\project`, and each
# `thinking` block is trimmed to 240 chars.
AGY_CAPTURE = Path(__file__).resolve().parents[2] / "fixtures" / "antigravity_transcript.jsonl"


def _live(sid: str = "s1", **extra: Any) -> Dict[str, Any]:
    row = {"session_id": sid, "kind": "pty", "agent": "claude", "alive": True,
           "project_dir": "E:/automation/proj", "started_at": 1_789_000_000}
    row.update(extra)
    return row


# ------------------------------------------------------- copilot_entries

COPILOT_CAPTURE = Path(__file__).resolve().parents[2] / "fixtures" / "copilot_events.jsonl"


# A conversation id as `build_resume_flags(..., session_id=...)` splices it.
_RESUMED = "51f1c38e-47c9-4220-9006-367a6dd29390"


# ----------------------------- #1393: a channel session, named by its bridge id

_BRIDGE = {"a": "session_01AAAAAAAAAAAAAAAAAAAAAA", "b": "session_01BBBBBBBBBBBBBBBBBBBBBB",
           "c": "session_01CCCCCCCCCCCCCCCCCCCCCC"}
_CONV = {"a": "3a999eec-ac85-4ee6-af21-998993027c9a", "b": "50f600b2-c3a6-47b9-99ad-be0c57792e16",
         "c": "23ed6350-8f51-49cd-9991-0a8bff9c92a6"}


def _registry_entry(root: Path, pid: int, key: str, **over: Any) -> None:
    """One ``~/.claude/sessions/<pid>.json``: Claude Code's own per-process
    record, naming its conversation and its Remote Control bridge."""
    entry = {"pid": pid, "sessionId": _CONV[key], "cwd": r"E:\work\project",
             "bridgeSessionId": _BRIDGE[key], "kind": "interactive"}
    entry.update(over)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{pid}.json").write_text(json.dumps(entry), encoding="utf-8")
