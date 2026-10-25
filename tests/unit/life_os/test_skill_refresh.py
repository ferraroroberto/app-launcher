"""``src/life_os/skill_refresh.py``: which files a skill's fingerprint covers,
the changed / unchanged / unknown decision, and the Settings toggle. Fictional
skills only; git is stubbed, so nothing here reads a real checkout."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from src.life_os import skill_refresh as refresh
from src.webapp_config import WebappConfig

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
CAPTURE = "2026-08-01-0900-fictional-topic.md"


def _skill(root: Path) -> Path:
    skills = root / ".claude" / "skills"
    skill = skills / "garden"
    for rel, text in {
        "SKILL.md": "# garden\nRead `.claude/skills/_shared/bootstrap.md` and `_shared/archive-skill.md`.\n",
        "description.md": "blurb",
        "maintenance.md": "upkeep",
        "assets/template.md": "template",
        "assets/logo.png": "not markdown",
        "conversations/2026-08-01-0900-fictional-topic.md": "a capture",
        "memory/notes.md": "private",
        "context/profile.md": "private",
    }.items():
        (skill / rel).parent.mkdir(parents=True, exist_ok=True)
        (skill / rel).write_text(text, encoding="utf-8")
    (skills / "_shared").mkdir()
    for name in ("bootstrap.md", "archive-skill.md", "unrelated.md"):
        (skills / "_shared" / name).write_text(name, encoding="utf-8")
    return skills


@pytest.fixture
def life_os(tmp_path: Path) -> Path:
    _skill(tmp_path / "life-os")
    return tmp_path / "life-os"


@pytest.fixture
def cfg(life_os: Path) -> WebappConfig:
    return WebappConfig(life_os_dir=str(life_os))


def _git(monkeypatch, output):
    calls = []
    def fake(repo, args, **_):
        calls.append(args)
        return output
    monkeypatch.setattr(refresh, "run_git", fake)
    return calls


class TestFingerprint:
    def test_covers_instructions_and_named_shared_guides_only(self, life_os):
        skills = life_os / ".claude" / "skills"
        files = refresh.instruction_files(skills, "garden")
        assert [p.relative_to(skills).as_posix() for p in files] == [
            "_shared/archive-skill.md", "_shared/bootstrap.md",
            "garden/SKILL.md", "garden/assets/template.md",
            "garden/description.md", "garden/maintenance.md",
        ]

    @pytest.mark.parametrize("rel", ["conversations/2026-08-01-0900-fictional-topic.md",
                                     "memory/notes.md", "context/profile.md",
                                     "../_shared/unrelated.md"])
    def test_excluded_files_do_not_move_it(self, life_os, rel):
        skills = life_os / ".claude" / "skills"
        before = refresh.skill_fingerprint(skills, refresh.instruction_files(skills, "garden"))
        (skills / "garden" / rel).write_text("edited", encoding="utf-8")
        after = refresh.skill_fingerprint(skills, refresh.instruction_files(skills, "garden"))
        assert before == after

    def test_content_moves_it_and_mtime_does_not(self, life_os):
        skills = life_os / ".claude" / "skills"
        target = skills / "_shared" / "bootstrap.md"
        def fp():
            return refresh.skill_fingerprint(skills, refresh.instruction_files(skills, "garden"))
        before = fp()
        os.utime(target, (1, 1))
        assert fp() == before
        target.write_text("new rules", encoding="utf-8")
        assert fp() != before

    def test_no_skill_md_is_unreadable(self, life_os):
        (life_os / ".claude" / "skills" / "garden" / "SKILL.md").unlink()
        assert refresh.instruction_files(life_os / ".claude" / "skills", "garden") is None


class TestDecision:
    def test_toggle_off_computes_and_records_nothing(self, cfg, monkeypatch):
        calls = _git(monkeypatch, "")
        plan = refresh.plan_refresh(replace(cfg, life_os_refresh_skill_instructions=False),
                                    "garden", SID, CAPTURE)
        assert plan == refresh.RefreshPlan("off")
        refresh.record_refresh(plan)
        assert calls == [] and not refresh.SKILL_HASHES_FILE.exists()

    def test_change_prompts_once_then_nothing(self, cfg, life_os, monkeypatch):
        _git(monkeypatch, None)
        refresh.record_refresh(refresh.plan_refresh(cfg, "garden", SID, CAPTURE))
        assert refresh.plan_refresh(cfg, "garden", SID, CAPTURE).suffix == ""
        (life_os / ".claude" / "skills" / "garden" / "SKILL.md").write_text("# new boundary", encoding="utf-8")
        first = refresh.plan_refresh(cfg, "garden", SID, CAPTURE)
        assert first.state == "changed"
        assert first.suffix == f' "{refresh.refresh_prompt("garden")}"'
        assert ".claude/skills/garden/SKILL.md" in first.suffix
        refresh.record_refresh(first)
        second = refresh.plan_refresh(cfg, "garden", SID.upper(), CAPTURE)
        assert (second.state, second.suffix) == ("unchanged", "")

    def test_the_prompt_passes_the_session_host_flags_allowlist(self, cfg, life_os, monkeypatch):
        from src.session_host import _validate_flags

        _git(monkeypatch, None)
        refresh.record_refresh(refresh.plan_refresh(cfg, "garden", SID, CAPTURE))
        (life_os / ".claude" / "skills" / "garden" / "SKILL.md").write_text("# new boundary", encoding="utf-8")
        plan = refresh.plan_refresh(cfg, "garden", SID, CAPTURE)
        assert plan.state == "changed"
        _validate_flags(f"--resume {SID} --remote-control{plan.suffix}")

    @pytest.mark.parametrize("commit, state", [
        (datetime(2026, 8, 1, 9, 30), "changed"),
        (datetime(2026, 8, 1, 8, 30), "unchanged"),
    ])
    def test_no_record_asks_git_since_the_capture_was_created(self, cfg, monkeypatch, commit, state):
        calls = _git(monkeypatch, str(int(commit.timestamp())))
        plan = refresh.plan_refresh(cfg, "garden", SID, CAPTURE)
        assert plan.state == state and bool(plan.suffix) == (state == "changed")
        assert calls[0][:3] == ["log", "-1", "--format=%ct"]
        assert ".claude/skills/garden/SKILL.md" in calls[0]
        assert not any("conversations/" in a or "memory/" in a for a in calls[0])

    def test_never_committed_is_unchanged(self, cfg, monkeypatch):
        _git(monkeypatch, "")
        assert refresh.plan_refresh(cfg, "garden", SID, CAPTURE).state == "unchanged"

    def test_git_unreadable_is_unknown_without_prompt(self, cfg, monkeypatch, caplog):
        _git(monkeypatch, None)
        with caplog.at_level("INFO", logger=refresh.__name__):
            plan = refresh.plan_refresh(cfg, "garden", SID, CAPTURE)
        assert (plan.state, plan.suffix) == ("unknown", "")
        assert "git history unreadable" in caplog.text and SID not in caplog.text

    def test_unreadable_fingerprint_is_unknown_and_not_recorded(self, cfg, life_os, monkeypatch, caplog):
        _git(monkeypatch, "")
        (life_os / ".claude" / "skills" / "garden" / "SKILL.md").unlink()
        with caplog.at_level("INFO", logger=refresh.__name__):
            plan = refresh.plan_refresh(cfg, "garden", SID, CAPTURE)
        assert (plan.state, plan.suffix) == ("unknown", "")
        assert "fingerprint unreadable" in caplog.text
        refresh.record_refresh(plan)
        assert not refresh.SKILL_HASHES_FILE.exists()

    def test_corrupt_store_falls_back_to_git(self, cfg, monkeypatch):
        refresh.SKILL_HASHES_FILE.write_text("{not json", encoding="utf-8")
        _git(monkeypatch, "")
        assert refresh.plan_refresh(cfg, "garden", SID, CAPTURE).state == "unchanged"

    def test_store_is_bounded(self, monkeypatch):
        monkeypatch.setattr(refresh, "MAX_RECORDS", 2)
        for n in range(3):
            refresh.record_refresh(refresh.RefreshPlan("unchanged", f"garden:{n}", "h"))
        assert list(json.loads(refresh.SKILL_HASHES_FILE.read_text(encoding="utf-8"))) == ["garden:1", "garden:2"]


@pytest.mark.parametrize("name, expected", [
    ("2026-08-01-0900-topic.md", datetime(2026, 8, 1, 9, 0)),
    ("2026-08-01-topic.md", datetime(2026, 8, 1)),
    ("topic.md", None),
])
def test_capture_created(name, expected):
    assert refresh.capture_created(name) == expected
