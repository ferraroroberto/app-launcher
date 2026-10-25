"""Selected-provider quota consumption (issue #847)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.usage import quota as quota_usage


def _window(
    name: str,
    duration: int,
    used: float | None,
    *,
    state: str = "available",
) -> dict:
    return {
        "id": name,
        "duration_minutes": duration,
        "used_percentage": used,
        "resets_at": "2026-09-09T18:00:00.000000Z",
        "state": state,
    }


def _observation(
    bucket: str,
    windows: list[dict],
    *,
    pool_id: str | None = None,
    state: str = "available",
) -> dict:
    return {
        "bucket": bucket,
        "account": {"key": "redacted", "state": "identified" if pool_id else "unknown"},
        "pool_id": pool_id,
        "observed_at": "2026-09-09T17:00:00.000000Z",
        "expires_at": "2026-09-09T17:10:00.000000Z",
        "state": state,
        "windows": windows,
    }


def _source(
    harness: str,
    provider: str,
    observations: list[dict],
    *,
    state: str = "available",
    reason: str = "native_observation",
) -> dict:
    return {
        "producer": harness + "-test",
        "harness": harness,
        "provider": provider,
        "state": state,
        "reason": reason,
        "checked_at": "2026-09-09T17:00:01.000000Z",
        "observations": observations,
    }


def _read(monkeypatch, snapshot: dict, selection: str, legacy=None) -> dict:
    """Build one view from ``snapshot``, through the code the app runs.

    This used to go via ``read_quota_view``, which had no production caller
    (#1004) — eight of this module's tests were exercising a wrapper nothing
    reached. They cover ``_view_from_snapshot``, which the live entry point
    ``read_quota_lines`` *does* reach, so the coverage is real and worth
    keeping; only the dead wrapper in front of it is gone. Calling the core
    directly keeps every assertion below pointed at code that runs.
    """
    monkeypatch.setattr(quota_usage, "_read_snapshot", lambda *_args: snapshot)
    route = quota_usage.resolve_quota_route(selection)
    return quota_usage._view_from_snapshot(
        snapshot, route, (lambda: legacy) if legacy is not None else None
    )


def test_codex_retains_independent_native_buckets_and_measured_zero(monkeypatch):
    observations = [
        _observation("default", [_window("primary", 10080, 0)]),
        _observation("reserve", [_window("secondary", 1440, 64)]),
        _observation("spark", [_window("primary", 300, None, state="unknown")], state="unknown"),
    ]
    view = _read(
        monkeypatch,
        {"sources": [_source("codex", "openai", observations)], "pools": []},
        "codex:gpt-6-astra",
    )

    assert view["state"] == "available"
    assert [item["bucket"] for item in view["observations"]] == ["default", "reserve", "spark"]
    assert view["observations"][0]["windows"][0] == {
        "id": "primary",
        "duration_minutes": 10080,
        "used_percentage": 0,
        "resets_at": "2026-09-09T18:00:00.000000Z",
        "state": "available",
    }
    assert view["observations"][2]["windows"][0]["used_percentage"] is None


@pytest.mark.parametrize("state", ["stale", "unknown", "unsupported", "error"])
def test_source_states_remain_distinct(monkeypatch, state):
    observations = [_observation("default", [_window("primary", 10080, 42, state=state)], state=state)]
    if state in {"unsupported", "error"}:
        observations = []
    view = _read(
        monkeypatch,
        {"sources": [_source("codex", "openai", observations, state=state, reason=state)], "pools": []},
        "codex:gpt-6-luna",
    )
    assert view["state"] == state
    assert view["available"] is False
    assert view["stale"] is (state == "stale")


def test_pi_does_not_alias_codex_by_provider_without_verified_adapter(monkeypatch):
    codex = _source(
        "codex", "openai",
        [_observation("default", [_window("primary", 10080, 22)], pool_id="pool-one")],
    )
    view = _read(
        monkeypatch,
        {"sources": [codex], "pools": [{"pool_id": "pool-one", "harnesses": ["codex"]}]},
        "pi:gpt-5.6-sol",
    )
    assert view["harness"] == "pi"
    assert view["provider"] == "openai"
    assert view["state"] == "unknown"
    assert view["observations"] == []


def test_verified_pi_adapter_can_show_shared_pool_without_ui_model_branch(monkeypatch):
    pi_observation = _observation(
        "default", [_window("primary", 10080, 22)], pool_id="pool-one"
    )
    view = _read(
        monkeypatch,
        {
            "sources": [_source("pi", "openai", [pi_observation])],
            "pools": [{"pool_id": "pool-one", "harnesses": ["codex", "pi"]}],
        },
        "pi:gpt-5.6-sol",
    )
    assert view["state"] == "available"
    assert view["observations"][0]["shared_account"] is True
    assert view["observations"][0]["harnesses"] == ["codex", "pi"]
    assert "pool_id" not in view["observations"][0]
    assert "account" not in view["observations"][0]


def test_legacy_claude_fallback_requires_absent_canonical_source_and_known_time(monkeypatch):
    absent = _source("claude", "anthropic", [], state="unknown", reason="source_absent")
    legacy = {
        "available": True, "stale": False, "updated_at": None,
        "five_hour": None, "seven_day": None,
    }
    view = _read(monkeypatch, {"sources": [absent], "pools": []}, "claude:opus", legacy)
    assert view["available"] is True  # old reader compatibility
    assert view["state"] == "unknown"  # never fresh without observed time

    broken = _source("claude", "anthropic", [], state="error", reason="source_unreadable")
    view = _read(monkeypatch, {"sources": [broken], "pools": []}, "claude:opus", legacy)
    assert view["state"] == "error"
    assert view["reason"] == "source_unreadable"


def test_canonical_reader_failure_never_falls_back_to_legacy(monkeypatch):
    def broken_reader(*_args):
        raise OSError("fixture")

    monkeypatch.setattr(quota_usage, "_read_snapshot", broken_reader)
    legacy = {
        "available": True, "stale": False, "updated_at": "2026-09-09T17:00:00Z",
        "five_hour": {"used_percentage": 12, "resets_at": 1788976800},
        "seven_day": None,
    }
    # Through the live entry point since #1004 removed the unreachable
    # ``read_quota_view`` wrapper this used to call. Asserting on the lines
    # is stronger: ``read_quota_lines`` promises a failed contract read
    # degrades *both* rows the same way, never silently one.
    lines = quota_usage.read_quota_lines(
        Path("fleet"), Path("state"), legacy_reader=lambda: legacy
    )
    assert lines, "a contract failure must still yield one row per harness"
    for line in lines:
        assert line["state"] == "error", line
        assert line["reason"] == "consumer_contract_unavailable", line


def test_stale_reset_passed_evidence_stays_historical(monkeypatch):
    stale_window = _window("primary", 10080, 42, state="stale")
    stale_window["resets_at"] = "2026-01-01T00:00:00.000000Z"
    view = _read(
        monkeypatch,
        {
            "sources": [_source(
                "codex", "openai",
                [_observation("default", [stale_window], state="stale")],
                state="stale", reason="observation_expired",
            )],
            "pools": [],
        },
        "codex:gpt-6-luna",
    )
    assert view["state"] == "stale"
    assert view["observations"][0]["windows"][0]["used_percentage"] == 42
    assert view["observations"][0]["windows"][0]["resets_at"].endswith("Z")


def test_refresh_gate_coalesces_and_rate_limits():
    gate = quota_usage.RefreshGate(cooldown_seconds=600)
    assert gate.begin(now=1000) is True
    assert gate.begin(now=1001) is False
    gate.finish()
    assert gate.begin(now=1599) is False
    assert gate.begin(now=1600) is True


def test_refresh_codex_invokes_canonical_one_shot(monkeypatch, tmp_path):
    fleet = tmp_path / "fleet"
    script = fleet / "skills" / "_lib" / "quota_sources.py"
    python = fleet / ".venv" / "Scripts" / "python.exe"
    script.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    script.write_text("# fixture", encoding="utf-8")
    python.write_text("", encoding="utf-8")
    seen = {}

    class Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return Result()

    monkeypatch.setattr(quota_usage.subprocess, "run", fake_run)
    assert quota_usage.refresh_codex(fleet, tmp_path / "state") == "complete"
    assert seen["argv"] == [
        str(python), str(script), "codex", "--state-dir", str(tmp_path / "state")
    ]
    assert seen["kwargs"]["timeout"] == 35
    assert seen["kwargs"]["creationflags"] == quota_usage.NO_WINDOW


# ------------------------------------------------- compact lines (#860)


def _lines(monkeypatch, snapshot: dict, legacy=None) -> list[dict]:
    monkeypatch.setattr(quota_usage, "_read_snapshot", lambda *_args: snapshot)
    return quota_usage.read_quota_lines(
        Path("fleet"), Path("state"),
        legacy_reader=(lambda: legacy) if legacy is not None else None,
    )


def test_lines_always_list_both_agents_in_fixed_order(monkeypatch):
    """Neither agent may drop out — the row set is not selection-scoped."""
    lines = _lines(monkeypatch, {"sources": [], "pools": []})
    assert [line["harness"] for line in lines] == ["claude", "codex"]
    assert [line["label"] for line in lines] == ["Claude Code", "Codex"]
    for line in lines:
        assert line["state"] == "unknown"
        assert line["five_hour"] is None and line["weekly"] is None


def test_lines_collapse_codex_buckets_to_the_fullest_window_per_duration(monkeypatch):
    """Real 2026-09-06 shape: three buckets, four windows, two numbers out."""
    observations = [
        _observation("codex_bengalfox", [
            _window("primary", 300, 0),
            _window("secondary", 10080, 0),
        ]),
        _observation("base_model_inference", [_window("primary", 10080, 0)]),
        _observation("codex", [_window("primary", 10080, 36)]),
    ]
    codex = _lines(
        monkeypatch,
        {"sources": [_source("codex", "openai", observations)], "pools": []},
    )[1]

    assert codex["state"] == "available"
    assert codex["five_hour"]["used_percentage"] == 0
    assert codex["weekly"]["used_percentage"] == 36


def test_lines_map_claude_native_window_ids_by_duration(monkeypatch):
    observations = [_observation("claude-code", [
        _window("five_hour", 300, 39),
        _window("seven_day", 10080, 19),
    ])]
    claude = _lines(
        monkeypatch,
        {"sources": [_source("claude", "anthropic", observations)], "pools": []},
    )[0]

    assert claude["five_hour"] == {
        "used_percentage": 39, "resets_at": "2026-09-09T18:00:00.000000Z",
        "duration_minutes": 300,
    }
    assert claude["weekly"]["used_percentage"] == 19
    assert claude["weekly"]["duration_minutes"] == 10080


def test_lines_never_promote_an_unmeasured_window_to_zero(monkeypatch):
    observations = [_observation(
        "claude-code", [_window("five_hour", 300, None, state="unknown")],
    )]
    claude = _lines(
        monkeypatch,
        {"sources": [_source("claude", "anthropic", observations)], "pools": []},
    )[0]
    assert claude["five_hour"] is None


def test_lines_ignore_a_duration_the_row_does_not_show(monkeypatch):
    observations = [_observation("reserve", [_window("secondary", 1440, 64)])]
    codex = _lines(
        monkeypatch,
        {"sources": [_source("codex", "openai", observations)], "pools": []},
    )[1]
    assert codex["five_hour"] is None and codex["weekly"] is None


def test_lines_degrade_one_agent_without_dropping_the_other(monkeypatch):
    observations = [_observation("codex", [_window("primary", 300, 12)])]
    lines = _lines(
        monkeypatch,
        {
            "sources": [
                _source("claude", "anthropic", [], state="error", reason="source_unreadable"),
                _source("codex", "openai", observations),
            ],
            "pools": [],
        },
    )
    assert [line["harness"] for line in lines] == ["claude", "codex"]
    assert lines[0]["state"] == "error"
    assert lines[1]["five_hour"]["used_percentage"] == 12


def test_lines_degrade_both_rows_when_the_contract_read_fails(monkeypatch):
    def boom(*_args):
        raise OSError("shard directory unreadable")

    monkeypatch.setattr(quota_usage, "_read_snapshot", boom)
    lines = quota_usage.read_quota_lines(Path("fleet"), Path("state"))
    assert [line["harness"] for line in lines] == ["claude", "codex"]
    for line in lines:
        assert line["state"] == "error"
        assert line["reason"] == "consumer_contract_unavailable"


# ------------------------------------------------------------ Grok's line


def _grok_session(home: Path, session_id: str, mtime: float) -> None:
    import os

    folder = home / "sessions" / "E%3A%5Crepo" / session_id
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text("{}", encoding="utf-8")
    os.utime(folder / "summary.json", (mtime, mtime))
    os.utime(folder, (mtime, mtime))


def _turn(ended: str, ticks: int) -> dict:
    return {"endedAt": ended, "costUsdTicks": ticks}


def test_grok_cost_today_sums_todays_turns_once(tmp_path):
    """Only turns that ended since local midnight count, and a turn a resumed
    or forked session inherited (same end time and cost) counts once."""
    from datetime import datetime, timezone

    now = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
    today = now.astimezone().replace(hour=1).timestamp()
    yesterday = today - 2 * 86400
    _grok_session(tmp_path, "a", today)
    _grok_session(tmp_path, "b", today)
    _grok_session(tmp_path, "old", yesterday)
    shared = _turn("2026-10-25T14:19:32.696693600+00:00", 9_000_000_000)
    ledgers = {
        "a": {"turns": [shared, _turn("2026-10-20T10:00:00Z", 50_000_000_000)]},
        "b": {"turns": [shared, _turn("2026-10-25T14:30:00Z", 1_000_000_000)]},
    }
    calls = []

    def runner(exe, session_id):
        calls.append(session_id)
        return ledgers[session_id]

    cost = quota_usage.grok_cost_today(tmp_path, "grok", now=now, runner=runner)
    assert sorted(calls) == ["a", "b"], "a session untouched today is never spawned for"
    assert cost == {"state": "available", "reason": "grok_usage_ledger", "usd": 1.0}


def test_grok_cost_today_is_unknown_when_any_ledger_fails(tmp_path):
    """A partial sum would understate the day, so one unreadable session makes
    the whole figure unknown rather than a smaller number."""
    from datetime import datetime, timezone

    now = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
    _grok_session(tmp_path, "a", now.timestamp())
    _grok_session(tmp_path, "b", now.timestamp())
    ledgers = {"a": {"turns": [_turn("2026-10-25T14:30:00Z", 1_000_000_000)]}, "b": None}

    cost = quota_usage.grok_cost_today(tmp_path, "grok", now=now, runner=lambda exe, sid: ledgers[sid])
    assert cost["state"] == "unknown"
    assert cost["usd"] is None


def test_grok_cost_today_without_the_cli_or_its_sessions(tmp_path):
    assert quota_usage.grok_cost_today(tmp_path, None)["state"] == "unsupported"
    missing = quota_usage.grok_cost_today(tmp_path, "grok", runner=lambda exe, sid: {})
    assert missing == {"state": "unsupported", "reason": "grok_sessions_absent", "usd": None}


def test_grok_line_never_carries_a_limit_window(monkeypatch):
    """Grok exposes no limits to scripts: its row has no windows, whatever
    the cost sweep finds, and the poll never waits on that sweep."""
    monkeypatch.setattr(quota_usage, "_grok_cache", {})
    started = []
    line = quota_usage.grok_quota_line(now=1000.0, start=started.append)
    assert line["five_hour"] is None and line["weekly"] is None
    assert line["state"] == "unsupported" and line["reason"] == "limits_not_exposed"
    assert line["cost_today"]["state"] == "unknown"
    assert len(started) == 1
    # A second poll while the sweep runs starts no second sweep.
    quota_usage.grok_quota_line(now=1001.0, start=started.append)
    assert len(started) == 1
