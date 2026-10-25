"""src.usage.auto_switch: when a Sonnet dispatch runs on Grok, and why not."""

from __future__ import annotations

import pytest

from src.usage import auto_switch

NOW = 1_800_000_000.0
# Half the week gone: pace 50.
_WEEKLY = {"resets_at": NOW + 3.5 * 86400, "duration_minutes": 10080}


def _line(used=None, **extra):
    weekly = dict(_WEEKLY, used_percentage=used) if used is not None else None
    return {"harness": "claude", "state": "available", "stale": False, "weekly": weekly, **extra}


def _decide(model="claude:sonnet", line=None, **kw):
    args = dict(enabled=True, claude_line=line, now=NOW, grok_installed=True, repo_trusted=True)
    args.update(kw)
    return auto_switch.decide(model, **args)


@pytest.mark.parametrize(("used", "outcome"), [
    (44, "kept"),       # more than 5 points under pace
    (45, "switched"),   # exactly 5 under counts as near
    (50, "switched"),
    (90, "switched"),
])
def test_pace_threshold(used, outcome):
    decision = _decide(line=_line(used))
    assert decision.outcome == outcome
    assert decision.model == ("grok:grok-4.5" if outcome == "switched" else "claude:sonnet")
    assert decision.pace_pct == 50


@pytest.mark.parametrize("model", ["claude:opus", "opus", "codex:gpt-5.6-sol", ""])
def test_only_the_sonnet_tier_ever_switches(model):
    assert _decide(model, line=_line(99)) == auto_switch.SwitchDecision(model)


def test_toggle_off_never_applies():
    assert _decide(line=_line(99), enabled=False).payload("claude:sonnet") is None


@pytest.mark.parametrize(("line", "reason"), [
    (None, "claude_reading_not_fresh"),
    (_line(99, stale=True), "claude_reading_not_fresh"),
    (_line(99, state="unknown"), "claude_reading_not_fresh"),
    (_line(None), "claude_weekly_unknown"),
    ({**_line(99), "weekly": {"used_percentage": 99, "resets_at": NOW - 60}}, "week_pace_unknown"),
])
def test_an_unknown_or_stale_reading_keeps_claude(line, reason):
    decision = _decide(line=line)
    assert (decision.model, decision.outcome, decision.reason) == ("claude:sonnet", "kept", reason)


@pytest.mark.parametrize(("kw", "reason"), [
    ({"grok_installed": False}, "grok_not_installed"),
    ({"repo_trusted": None}, "grok_trust_unreadable"),
    ({"repo_trusted": False}, "repo_not_trusted_by_grok"),
])
def test_a_blocked_switch_is_skipped_with_its_reason(kw, reason):
    decision = _decide(line=_line(80), **kw)
    assert (decision.model, decision.outcome, decision.reason) == ("claude:sonnet", "skipped", reason)


def test_grok_trusts_exact_folder_only(tmp_path):
    repo = tmp_path / "repo"
    store = tmp_path / "trusted_folders.toml"
    assert auto_switch.grok_trusts(repo, tmp_path) is False
    store.write_text(f"[folders.'{tmp_path}']\ntrusted = true\n", encoding="utf-8")
    assert auto_switch.grok_trusts(repo, tmp_path) is False
    store.write_text(f"[folders.'{repo}']\ntrusted = true\n", encoding="utf-8")
    assert auto_switch.grok_trusts(repo, tmp_path) is True
    store.write_text("not = [toml", encoding="utf-8")
    assert auto_switch.grok_trusts(repo, tmp_path) is None
