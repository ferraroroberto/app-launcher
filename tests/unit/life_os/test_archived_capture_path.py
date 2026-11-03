"""An archived Life OS capture resolves like a flat one; anything else stays refused."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.webapp.routers.life_os_conversations import _conversation_path, _conversation_rows


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "life"
    conv = root / ".claude" / "skills" / "demo" / "conversations"
    (conv / "archive" / "deeper").mkdir(parents=True)
    (conv / "2026-01-01-0900-flat.md").write_text("x", encoding="utf-8")
    (conv / "archive" / "2026-01-01-0900-old.md").write_text("x", encoding="utf-8")
    (conv / "archive" / "deeper" / "2026-01-01-0900-deep.md").write_text("x", encoding="utf-8")
    (conv / "archive" / "index.md").write_text("x", encoding="utf-8")
    (root / ".claude" / "skills" / "other" / "conversations").mkdir(parents=True)
    (root / ".claude" / "skills" / "other" / "conversations" / "a.md").write_text("x", encoding="utf-8")
    skill = SimpleNamespace(id="demo", skill_dir=root / ".claude" / "skills" / "demo")
    return root, skill


BASE = ".claude/skills/demo/conversations"


def test_flat_capture_still_accepted(tree):
    root, skill = tree
    assert _conversation_path(root, skill, f"{BASE}/2026-01-01-0900-flat.md") is not None


def test_archived_capture_accepted(tree):
    root, skill = tree
    got = _conversation_path(root, skill, f"{BASE}/archive/2026-01-01-0900-old.md")
    assert got is not None and got.parent.name == "archive"


@pytest.mark.parametrize("rel", [
    f"{BASE}/archive/deeper/2026-01-01-0900-deep.md",
    f"{BASE}/archive/index.md",
    ".claude/skills/other/conversations/a.md",
    f"{BASE}/../../other/conversations/a.md",
    "C:/Windows/win.md",
])
def test_everything_else_refused(tree, rel):
    root, skill = tree
    assert _conversation_path(root, skill, rel) is None


def test_row_for_archived_file_resolves_through_archive(tree):
    root, skill = tree
    cfg = SimpleNamespace(life_os_dir=str(root), fleet_config_dir=str(root / "nofleet"))
    rows = _conversation_rows(cfg, [{"file": "2026-01-01-0900-old.md"}], skill=skill)
    assert rows[0]["resume_reason"] not in (
        "Capture path is outside this skill.", "Capture is missing.")
