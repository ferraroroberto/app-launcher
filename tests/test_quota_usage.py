"""Selected-provider quota consumption (issue #847)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import quota_usage


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
    monkeypatch.setattr(quota_usage, "_read_snapshot", lambda *_args: snapshot)
    return quota_usage.read_quota_view(
        Path("fleet"), Path("state"), selection,
        legacy_reader=(lambda: legacy) if legacy is not None else None,
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
        "codex:gpt-5.6-luna",
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
    view = quota_usage.read_quota_view(
        Path("fleet"), Path("state"), "claude:opus", legacy_reader=lambda: legacy
    )
    assert view["state"] == "error"
    assert view["reason"] == "consumer_contract_unavailable"
    assert view["observations"] == []


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
        "codex:gpt-5.6-luna",
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
