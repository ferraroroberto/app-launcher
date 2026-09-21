"""Brief-file storage bounds for Board issue-start dispatch briefs (#1114)."""

from __future__ import annotations

import os
import time

import pytest

from src import dispatch_brief


@pytest.fixture
def briefs_dir(tmp_path, monkeypatch):
    d = tmp_path / "briefs"
    monkeypatch.setattr(dispatch_brief, "BRIEFS_DIR", d)
    return d


def _aged(path, seconds_ago: float) -> None:
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


def test_write_prunes_expired_briefs(briefs_dir):
    old = dispatch_brief.write_brief("old scope")
    _aged(old, dispatch_brief.BRIEF_TTL + 60)
    fresh = dispatch_brief.write_brief("recent scope")
    _aged(fresh, 60)

    new = dispatch_brief.write_brief("new scope")

    assert not old.exists()
    assert fresh.exists() and new.exists()


def test_count_cap_keeps_only_the_newest(briefs_dir, monkeypatch):
    monkeypatch.setattr(dispatch_brief, "MAX_BRIEF_FILES", 3)
    paths = []
    for i in range(5):
        p = dispatch_brief.write_brief(f"brief {i}")
        _aged(p, 100 - i)  # later writes are newer
        paths.append(p)

    dispatch_brief.prune_briefs()

    assert [p.exists() for p in paths] == [False, False, True, True, True]


def test_unsafe_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch_brief, "BRIEFS_DIR", tmp_path / "a b&c")
    with pytest.raises(RuntimeError, match="not command-line safe"):
        dispatch_brief.write_brief("scope")
    assert list((tmp_path / "a b&c").glob("*.md")) == []


def test_discard_is_idempotent(briefs_dir):
    p = dispatch_brief.write_brief("scope")
    dispatch_brief.discard_brief(p)
    dispatch_brief.discard_brief(p)
    assert not p.exists()
