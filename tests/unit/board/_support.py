"""Fixtures-as-data shared by the Board unit modules: clock, session and transcript row builders."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ merge_sessions


def _live(session_id: str, project_dir: str, started_min_ago: int, **extra):
    row = {
        "session_id": session_id,
        "kind": "pty",
        "agent": "claude",
        "project_dir": project_dir,
        "name": Path(project_dir).name,
        "alive": True,
        "started_at": _iso(NOW - timedelta(minutes=started_min_ago)),
        "live_title": "",
        "prompt_title": "",
    }
    row.update(extra)
    return row


def _state_row(cwd: str, status: str = "working", updated_min_ago: int = 5, **extra):
    row = {
        "project": Path(cwd).name,
        "status": status,
        "transcript_path": None,
        "cwd": cwd,
        "updated_at": _iso(NOW - timedelta(minutes=updated_min_ago)),
    }
    row.update(extra)
    return row


# ------------------------------- transcript activity overlay (#305 / #309)


def _msg_line(kind: str, ts: datetime) -> str:
    """One real conversation line — the shape the #309 tail probe accepts."""
    return json.dumps({
        "type": kind,
        "timestamp": _iso(ts),
        "message": {"role": kind, "content": [{"type": "text", "text": "hi"}]},
    })


def _transcript_file(tmp_path: Path, mtime: datetime, content: str = None) -> str:
    """A transcript file with its mtime pinned to ``mtime``.

    Default content is a single assistant line stamped at ``mtime``, so mtime
    and last-activity agree (the plain #305 shape). Pass ``content`` to make
    them diverge — the #309 metadata-only-appends case.
    """
    target = tmp_path / "transcript.jsonl"
    if content is None:
        content = _msg_line("assistant", mtime) + "\n"
    target.write_text(content, encoding="utf-8")
    stamp = mtime.timestamp()
    os.utime(target, (stamp, stamp))
    return str(target)


# A stand-in for fleet-config's skills/_lib/active_issue.py contract: the real
# classifier's semantics are fleet-config#852's to test; these pin only how
# the Board consumes whatever verdict it returns.
_FAKE_CLAIM_CONTRACT = '''
def current_host():
    return "HOST"

def pid_state(pid):
    return None

def classify_owner(row, *, live_session_ids, pid_probe, host):
    assert pid_probe is pid_state and host == "HOST"
    sid = row.get("owner_session_id")
    if sid == "raise":
        raise RuntimeError("boom")
    if sid == "bogus":
        return "zombie"
    if not sid or live_session_ids is None:
        return "unknown"
    return "live" if sid in live_session_ids else "dead"
'''


def _claim_contract(tmp_path: Path) -> Path:
    fleet_config = tmp_path / "fleet-config"
    lib = fleet_config / "skills" / "_lib"
    lib.mkdir(parents=True)
    (lib / "active_issue.py").write_text(_FAKE_CLAIM_CONTRACT, encoding="utf-8")
    return fleet_config


# --------------------------------- pending background dispatch overlay (#464)


def _tool_result_line(ts: datetime, result: dict) -> str:
    """One tool_result line whose ``toolUseResult`` carries a background
    dispatch's synchronous launch ack — the shape ``_launched_background_ids``
    reads. ``toolUseResult`` is a sibling of ``message``, not inside it, per
    live transcripts."""
    return json.dumps({
        "type": "user",
        "timestamp": _iso(ts),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ack"}],
        },
        "toolUseResult": result,
    })


# ------------------------------------------------------------- github_client


_CANNED_ISSUE = {
    "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
    "number": 164, "title": "Board tab", "url": "https://github.com/x/164",
    "updatedAt": "2026-07-01T10:00:00Z",
    "labels": [{"name": "enhancement"}],
}
_CANNED_PR = {
    "repository": {"nameWithOwner": "ferraroroberto/photo-ocr"},
    "number": 67, "title": "fix chunk merge", "url": "https://github.com/x/67",
    "updatedAt": "2026-07-02T08:00:00Z", "isDraft": False,
}


class _FakeGh:
    """subprocess.run stand-in keyed on the gh subcommand + filters."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        rows = []
        if "prs" in argv and "--merged" in argv:
            rows = []                      # nothing merged today
        elif "prs" in argv:
            rows = [_CANNED_PR]
        elif "issues" in argv and "closed" in argv:
            rows = []                      # nothing closed today
        elif "issues" in argv:
            rows = [_CANNED_ISSUE]
        completed = subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")
        return completed


# ------------------------ pending background dispatch, tool-use-id keyed (#576)


def _bash_tool_use_line(
    ts: datetime, tool_use_id: str, *, run_in_background: bool = True, tool_name: str = "Bash",
) -> str:
    """One assistant line dispatching a ``Bash`` (or, per #594, ``PowerShell``)
    tool call — the shape ``_launched_bash_dispatch_ids`` reads. This is the
    tool-call-id-keyed counterpart to ``_tool_result_line``'s
    ``toolUseResult``-keyed shape: #576 found real transcripts no longer
    carry ``backgroundTaskId`` on the result, so the launch has to be read
    off the ``tool_use`` block itself."""
    return json.dumps({
        "type": "assistant",
        "timestamp": _iso(ts),
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": {"command": "pwsh -File scripts/verify-before-ship.ps1",
                           "run_in_background": run_in_background},
            }],
        },
    })


def _tool_use_notification_line(ts: datetime, task_id: str, tool_use_id: str) -> str:
    """A completion notice carrying both correlation tags real transcripts
    use — ``<task-id>`` (the legacy scheme) and ``<tool-use-id>`` (#576).
    Paired with an immediate ``dequeue``, matching real delivery (#601) —
    see :func:`_task_notification_line`."""
    enqueue = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": _iso(ts),
        "content": (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    })
    dequeue = json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": _iso(ts)})
    return enqueue + "\n" + dequeue


# ------------------------------------------------------------ jobs_attention


def _seed_job(overrides: dict, job_id: str, run: dict) -> None:
    jobs_path = overrides["tmp_jobs_path"]
    existing = json.loads(jobs_path.read_text(encoding="utf-8")) if jobs_path.exists() else {"jobs": []}
    existing["jobs"].append({
        "id": job_id,
        "name": job_id.replace("_", " "),
        "script_path": "C:/nowhere/script.py",
    })
    jobs_path.write_text(json.dumps(existing), encoding="utf-8")

    run_id = run.get("run_id", "20260702T090000")
    run_dir = overrides["tmp_jobs_runs_dir"] / job_id / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_id, "job_id": job_id, **run}), encoding="utf-8"
    )
