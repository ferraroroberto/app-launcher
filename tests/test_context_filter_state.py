"""src/context_filter_state.py — mode.json + shadow.jsonl IO (issue #713).

Mirrors board_state.py's own test style: absent/corrupt/BOM/wrong-type files
degrade to an ``available: False`` (or, for the mode file specifically, a
true "off" default on an absent file) shape and never raise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.context_filter_state import (
    HARNESS_SUPPORT,
    VALID_MODES,
    read_mode,
    read_stats,
    write_mode,
)


class TestReadMode:
    def test_absent_file_resolves_to_off_available(self, tmp_path: Path):
        result = read_mode(tmp_path / "mode.json")
        assert result == {
            "available": True,
            "mode": "off",
            "updated_at": None,
            "updated_by": None,
        }

    def test_valid_file_read_back(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        path.write_text(
            json.dumps({"mode": "shadow", "updated_at": "2026-08-01T00:00:00Z", "updated_by": "x"}),
            encoding="utf-8",
        )
        result = read_mode(path)
        assert result == {
            "available": True,
            "mode": "shadow",
            "updated_at": "2026-08-01T00:00:00Z",
            "updated_by": "x",
        }

    def test_corrupt_json_is_unavailable(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        path.write_text("not json at all {{{", encoding="utf-8")
        result = read_mode(path)
        assert result["available"] is False
        assert result["mode"] is None

    def test_bom_is_tolerated(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        payload = json.dumps({"mode": "rewrite"}).encode("utf-8-sig")
        path.write_bytes(payload)
        result = read_mode(path)
        assert result["available"] is True
        assert result["mode"] == "rewrite"

    def test_wrong_type_is_unavailable(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        path.write_text(json.dumps(["off"]), encoding="utf-8")
        result = read_mode(path)
        assert result["available"] is False

    def test_invalid_mode_value_is_unavailable(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        path.write_text(json.dumps({"mode": "bogus"}), encoding="utf-8")
        result = read_mode(path)
        assert result["available"] is False

    def test_unreadable_directory_is_unavailable(self, tmp_path: Path):
        # A path that exists but is a directory, not a file, mirrors the
        # "unreadable" branch without needing OS permission trickery.
        path = tmp_path / "mode.json"
        path.mkdir()
        result = read_mode(path)
        assert result == {
            "available": False,
            "mode": None,
            "updated_at": None,
            "updated_by": None,
        }


class TestWriteMode:
    def test_rejects_invalid_mode(self, tmp_path: Path):
        with pytest.raises(ValueError):
            write_mode(tmp_path / "mode.json", "bogus")

    def test_write_then_read_round_trips(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        result = write_mode(path, "shadow", updated_by="tester")
        assert result["available"] is True
        assert result["mode"] == "shadow"
        assert result["updated_by"] == "tester"
        assert result["updated_at"]
        # Confirm it's a fresh read_mode(), not just an echo of the payload.
        assert read_mode(path) == result

    def test_creates_parent_directory(self, tmp_path: Path):
        path = tmp_path / "nested" / "dir" / "mode.json"
        write_mode(path, "off")
        assert path.exists()

    def test_no_orphaned_tmp_file_on_success(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        write_mode(path, "rewrite")
        assert not list(tmp_path.glob("*.tmp"))

    def test_no_orphaned_tmp_file_when_validation_fails_before_write(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        with pytest.raises(ValueError):
            write_mode(path, "invalid-mode")
        assert not path.exists()
        assert not list(tmp_path.glob("*.tmp"))

    def test_every_valid_mode_round_trips(self, tmp_path: Path):
        for mode in VALID_MODES:
            path = tmp_path / f"mode-{mode}.json"
            result = write_mode(path, mode)
            assert result["mode"] == mode


class TestReadStats:
    def test_absent_file_is_unavailable(self, tmp_path: Path):
        result = read_stats(tmp_path / "shadow.jsonl")
        assert result["available"] is False
        assert result["totals"] == {
            "rows": 0, "raw_tokens": 0, "compressed_tokens": 0, "tokens_saved": 0,
        }

    def test_empty_file_is_available_with_zero_totals(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        path.write_text("", encoding="utf-8")
        result = read_stats(path)
        assert result["available"] is True
        assert result["totals"]["rows"] == 0

    def test_bad_lines_are_skipped_not_fatal(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        lines = [
            "not json {{{",
            json.dumps("a string, not an object"),
            json.dumps({"ts": "2026-08-01T00:00:00Z", "agent": "claude", "raw_tokens": 100, "compressed_tokens": 40}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = read_stats(path)
        assert result["available"] is True
        assert result["totals"]["rows"] == 1
        assert result["totals"]["tokens_saved"] == 60

    def test_missing_agent_defaults_to_claude(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        row = {"ts": "2026-08-01T00:00:00Z", "raw_tokens": 100, "compressed_tokens": 50}
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        result = read_stats(path)
        assert set(result["per_agent"].keys()) == {"claude"}
        assert result["per_agent"]["claude"]["tokens_saved"] == 50

    def test_missing_ts_counts_in_totals_not_day_windows(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        row = {"agent": "codex", "raw_tokens": 200, "compressed_tokens": 100}
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        result = read_stats(path)
        assert result["totals"]["rows"] == 1
        assert result["totals"]["tokens_saved"] == 100
        assert result["last_7_days"]["rows"] == 0
        assert result["today"]["rows"] == 0

    def test_day_window_slicing_with_fabricated_ts(self, tmp_path: Path, monkeypatch):
        import src.context_filter_state as cfs
        from datetime import datetime, timezone

        fixed_now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(cfs, "_now", lambda: fixed_now)

        path = tmp_path / "shadow.jsonl"
        rows = [
            # today
            {"ts": "2026-08-02T01:00:00Z", "agent": "claude", "raw_tokens": 100, "compressed_tokens": 50},
            # within last 7 days, not today
            {"ts": "2026-07-28T01:00:00Z", "agent": "claude", "raw_tokens": 100, "compressed_tokens": 50},
            # older than 7 days
            {"ts": "2026-07-01T01:00:00Z", "agent": "claude", "raw_tokens": 100, "compressed_tokens": 50},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        result = read_stats(path)
        assert result["today"]["rows"] == 1
        assert result["last_7_days"]["rows"] == 2
        assert result["totals"]["rows"] == 3

    def test_command_family_git_and_gh_include_subcommand(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        rows = [
            {"ts": "2026-08-01T00:00:00Z", "command": "git commit -m x"},
            {"ts": "2026-08-01T00:00:00Z", "command": "git status"},
            {"ts": "2026-08-01T00:00:00Z", "command": "gh pr view 5"},
            {"ts": "2026-08-01T00:00:00Z", "command": "ls -la"},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        result = read_stats(path)
        families = {c["command"] for c in result["top_commands"]}
        assert "git commit" in families
        assert "git status" in families
        assert "gh pr" in families
        assert "ls" in families

    def test_top_commands_capped_at_five(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        rows = [{"ts": "2026-08-01T00:00:00Z", "command": f"cmd{i}"} for i in range(8)]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        result = read_stats(path)
        assert len(result["top_commands"]) == 5

    def test_cache_invalidates_on_mtime_change(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        path.write_text(
            json.dumps({"ts": "2026-08-01T00:00:00Z", "raw_tokens": 100, "compressed_tokens": 50}) + "\n",
            encoding="utf-8",
        )
        first = read_stats(path)
        assert first["totals"]["rows"] == 1

        # Append another row — different size, so the (mtime_ns, size) cache
        # key must change even if the filesystem's mtime clock is coarse.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "2026-08-01T00:00:00Z", "raw_tokens": 100, "compressed_tokens": 50}) + "\n")
        second = read_stats(path)
        assert second["totals"]["rows"] == 2

    def test_cache_reused_when_file_unchanged(self, tmp_path: Path, monkeypatch):
        import src.context_filter_state as cfs

        path = tmp_path / "shadow.jsonl"
        path.write_text(
            json.dumps({"ts": "2026-08-01T00:00:00Z", "raw_tokens": 100, "compressed_tokens": 50}) + "\n",
            encoding="utf-8",
        )
        read_stats(path)
        calls = []
        original = cfs._compute_stats
        def spy(p):
            calls.append(p)
            return original(p)
        monkeypatch.setattr(cfs, "_compute_stats", spy)
        read_stats(path)  # unchanged file — must not recompute
        assert calls == []

    def test_tokens_saved_ignores_rows_missing_either_token_field(self, tmp_path: Path):
        path = tmp_path / "shadow.jsonl"
        rows = [
            {"ts": "2026-08-01T00:00:00Z", "raw_tokens": 100},  # no compressed_tokens
            {"ts": "2026-08-01T00:00:00Z", "compressed_tokens": 50},  # no raw_tokens
            {"ts": "2026-08-01T00:00:00Z", "raw_tokens": 100, "compressed_tokens": 40},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        result = read_stats(path)
        assert result["totals"]["rows"] == 3
        assert result["totals"]["raw_tokens"] == 100
        assert result["totals"]["compressed_tokens"] == 40
        assert result["totals"]["tokens_saved"] == 60


class TestHarnessSupport:
    def test_shape(self):
        assert isinstance(HARNESS_SUPPORT, list)
        ids = {h["id"] for h in HARNESS_SUPPORT}
        assert ids == {"claude", "codex", "grok", "pi", "copilot", "antigravity"}
        for h in HARNESS_SUPPORT:
            assert h["status"] in ("active", "unsupported", "planned")

    def test_active_and_unsupported_and_planned_present(self):
        by_id = {h["id"]: h for h in HARNESS_SUPPORT}
        assert by_id["claude"]["status"] == "active"
        assert by_id["codex"]["status"] == "active"
        assert by_id["grok"]["status"] == "unsupported"
        assert by_id["pi"]["status"] == "active"  # fleet-config#545 shipped
        assert by_id["copilot"]["status"] == "planned"
        assert by_id["antigravity"]["status"] == "active"  # fleet-config#546 shipped
