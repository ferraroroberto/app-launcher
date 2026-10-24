"""Board backend: the sessions-state, active-issue and rate-limit file readers."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from src import board

from tests.unit.board._support import NOW, _iso


# ------------------------------------------------------ read_sessions_state


def test_state_missing_file_unavailable(tmp_path: Path):
    result = board.read_sessions_state(tmp_path / "nope.json", now=NOW)
    assert result == {"available": False, "stale": False, "updated_at": None, "rows": {}}


def test_state_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text("{not json", encoding="utf-8")
    assert board.read_sessions_state(target, now=NOW)["available"] is False


def test_state_fresh_rows(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text(json.dumps({
        "sid-1": {"project": "photo-ocr", "status": "needs-you",
                  "cwd": "E:/automation/photo-ocr",
                  "updated_at": _iso(NOW - timedelta(minutes=12))},
    }), encoding="utf-8")
    result = board.read_sessions_state(target, now=NOW)
    assert result["available"] is True
    assert result["stale"] is False
    assert set(result["rows"]) == {"sid-1"}


def test_state_stale_when_newest_row_old(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text(json.dumps({
        "sid-1": {"status": "working", "cwd": "x",
                  "updated_at": _iso(NOW - timedelta(hours=30))},
    }), encoding="utf-8")
    assert board.read_sessions_state(target, now=NOW)["stale"] is True


# ------------------------------------------------------ read_active_issues


def test_active_issues_missing_or_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "active-issues.json"
    assert board.read_active_issues(target, now=NOW) == {
        "available": False, "updated_at": None, "rows": {},
    }
    target.write_text("{not json", encoding="utf-8")
    assert board.read_active_issues(target, now=NOW)["available"] is False


def test_active_issues_keeps_fresh_and_expires_orphans(tmp_path: Path):
    target = tmp_path / "active-issues.json"
    target.write_text(json.dumps({
        "APP-LAUNCHER#528": {
            "repo": "app-launcher", "number": 528,
            "branch": "feat/528-active", "started_at": _iso(NOW - timedelta(hours=2)),
        },
        "photo-ocr#73": {
            "repo": "photo-ocr", "number": 73,
            "branch": "fix/73-old", "started_at": _iso(NOW - timedelta(hours=25)),
        },
        "broken#1": {"repo": "broken", "number": 1, "started_at": "garbage"},
    }), encoding="utf-8")

    result = board.read_active_issues(target, now=NOW)
    assert result["available"] is True
    assert result["updated_at"] == _iso(NOW - timedelta(hours=2))
    assert set(result["rows"]) == {"app-launcher#528"}


# -------------------------------------------------------- read_rate_limits


_EMPTY_RATE_LIMITS = {
    "available": False, "stale": False, "updated_at": None,
    "five_hour": None, "seven_day": None,
}


def test_rate_limits_missing_file_unavailable(tmp_path: Path):
    result = board.read_rate_limits(tmp_path / "nope.json", now=NOW)
    assert result == _EMPTY_RATE_LIMITS


def test_rate_limits_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text("{not json", encoding="utf-8")
    assert board.read_rate_limits(target, now=NOW)["available"] is False


def test_rate_limits_fresh_both_windows(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": 42, "resets_at": 1751640000},
        "seven_day": {"used_percentage": 77, "resets_at": 1751900000},
        "captured_at": _iso(NOW - timedelta(minutes=2)),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["five_hour"] == {"used_percentage": 42, "resets_at": 1751640000}
    assert result["seven_day"] == {"used_percentage": 77, "resets_at": 1751900000}


def test_rate_limits_stale_when_captured_at_old(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": 10, "resets_at": 1751640000},
        "captured_at": _iso(NOW - timedelta(minutes=45)),
    }), encoding="utf-8")
    assert board.read_rate_limits(target, now=NOW)["stale"] is True


def test_rate_limits_one_window_absent(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": 10, "resets_at": 1751640000},
        "captured_at": _iso(NOW),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["five_hour"] == {"used_percentage": 10, "resets_at": 1751640000}
    assert result["seven_day"] is None


def test_rate_limits_window_is_null(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": None,
        "seven_day": {"used_percentage": 5, "resets_at": 1751900000},
        "captured_at": _iso(NOW),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["five_hour"] is None
    assert result["seven_day"] == {"used_percentage": 5, "resets_at": 1751900000}


def test_rate_limits_tolerates_utf8_bom(tmp_path: Path):
    # The fleet-config statusline writer is PowerShell; .NET's
    # [System.Text.Encoding]::UTF8 defaults to emitting a BOM (fleet-config#259
    # found this the hard way). A BOM'd file must still parse, not read as
    # corrupt.
    target = tmp_path / "rate-limits.json"
    payload = json.dumps({
        "five_hour": {"used_percentage": 41.7, "resets_at": 1751640000},
        "captured_at": _iso(NOW),
    })
    target.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    result = board.read_rate_limits(target, now=NOW)
    assert result["available"] is True
    assert result["five_hour"] == {"used_percentage": 41.7, "resets_at": 1751640000}


def test_rate_limits_window_present_but_null_fields(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": None, "resets_at": None},
        "captured_at": _iso(NOW),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["available"] is True
    assert result["five_hour"] == {"used_percentage": None, "resets_at": None}
