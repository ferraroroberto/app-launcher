"""Shared builders for the fleet-chief tests: state rows, transcripts and the remembered pointer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


from src import chief_pointer


def _chief_row(**extra):
    row = {
        "session_id": "chief-old",
        "kind": "pty",
        "agent": "claude",
        "label": "chief",
        "name": "chief",
        "alive": True,
    }
    row.update(extra)
    return row


def _chief_state_row(session_id, updated_at, **extra):
    row = {
        "launcher_session_id": session_id, "agent": "claude",
        "name": "chief", "cwd": "E:/automation/fleet-config",
        "updated_at": updated_at,
    }
    row.update(extra)
    return row


def _transcript(tmp_path: Path, stem: str, *, typed: bool) -> str:
    """A chief transcript JSONL, with or without a typed human prompt (#670).

    The bootstrap lines mirror what a launcher-spawned, never-talked-to chief
    actually writes (verified against the real 2026-07-28 blank chief): the
    slash-command wrapper as a plain string, the skill body it expands to as
    a content *list*, and the post-spawn rename as a ``<system-reminder>`` —
    three user lines, none of them typed by a person.
    """
    lines = [
        {"type": "user", "timestamp": "2026-07-28T19:12:55Z", "message": {
            "role": "user",
            "content": "<command-message>chief</command-message>\n"
                       "<command-name>/chief</command-name>"}},
        {"type": "user", "timestamp": "2026-07-28T19:12:55Z", "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Base directory for this skill: ..."}]}},
        {"type": "assistant", "timestamp": "2026-07-28T19:13:20Z", "message": {
            "id": "m1", "role": "assistant",
            "content": [{"type": "text", "text": "read the handover"}]}},
        {"type": "user", "timestamp": "2026-07-28T19:22:21Z", "message": {
            "role": "user",
            "content": '<system-reminder>The user named this session "chief".'
                       "</system-reminder>"}},
    ]
    if typed:
        lines.append({
            "type": "user", "timestamp": "2026-07-28T19:30:00Z",
            "message": {"role": "user", "content": "how is it going?"}})
    target = tmp_path / f"{stem}.jsonl"
    target.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return str(target)


def _recent_iso(**delta_kwargs) -> str:
    """An ISO-8601 UTC stamp ``delta_kwargs`` in the past from the *real*
    current time (issue #658) — for rows exercised through the HTTP-level
    ``/api/board/chief/ensure`` endpoint, which calls ``board._now()``
    internally with no way to inject a frozen clock. A hardcoded calendar
    date here goes permanently stale the moment real wall-clock time
    crosses ``board.STATE_STALE_AFTER`` (24h) past it — this stays valid
    on any run date. Tests that already control ``now=`` directly (see
    ``test_stale_row_past_24h_is_excluded``) don't need this."""
    stamp = datetime.now(timezone.utc) - timedelta(**delta_kwargs)
    return stamp.isoformat().replace("+00:00", "Z")


def _chief(*, transcript=None, hours=0, minutes=0, **extra):
    """One state-row spec for the ranker table: how old, and what its
    transcript holds (``typed`` / ``blank`` / ``missing``, or none at all)."""
    return {"age": {"hours": hours, "minutes": minutes}, "transcript": transcript, **extra}


def _write_pointer(session_id, transcript_path, *, age=None, source="live"):
    """Put a pointer on disk with an explicit ``seen_at`` age.

    Writes raw rather than going through ``write_chief_pointer`` because these
    tests care about a pointer that was stamped days ago — the writer always
    stamps *now*. The path comes from the module global the autouse
    ``_isolated_chief_pointer`` fixture has already redirected at ``tmp_path``.
    """
    seen = datetime.now(timezone.utc) - (age or timedelta(minutes=1))
    chief_pointer.CHIEF_POINTER_FILE.write_text(
        json.dumps({
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "seen_at": seen.isoformat().replace("+00:00", "Z"),
            "source": source,
        }),
        encoding="utf-8",
    )
