"""Shared builders for the Board drill-down tests: transcript lines and live sessions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path




def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ last_exchange


def _write_jsonl(path: Path, lines: list) -> str:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return str(path)


def _user_line(text) -> dict:
    return {
        "type": "user", "timestamp": "2026-07-02T11:50:00Z",
        "message": {"role": "user", "content": text},
    }


def _assistant_line(blocks: list, msg_id: str = "m1") -> dict:
    return {
        "type": "assistant", "timestamp": "2026-07-02T11:55:00Z",
        "message": {"id": msg_id, "role": "assistant", "content": blocks},
    }


# --------------------------------------------------- state_row_for_session


def _live_sess(session_id: str, project_dir: str, started_min_ago: int) -> dict:
    return {
        "session_id": session_id,
        "kind": "pty",
        "alive": True,
        "project_dir": project_dir,
        "started_at": _iso(NOW - timedelta(minutes=started_min_ago)),
    }
