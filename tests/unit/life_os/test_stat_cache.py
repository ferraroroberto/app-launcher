"""DirListingCache: an unchanged folder costs a stat per folder, never a re-read of the listing.

Synthetic folders under ``tmp_path`` only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest

from app.webapp.routers import life_os_files
from src.life_os import stat_cache
from src.life_os.stat_cache import DirListingCache


class _Builds:
    """A build function that counts how often it ran."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.count = 0

    def __call__(self) -> List[str]:
        self.count += 1
        return sorted(p.name for p in self.root.rglob("*") if p.is_file())


@pytest.fixture()
def stat_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> List[str]:
    """Every ``os.stat`` the cache makes on a path under ``tmp_path``."""
    seen: List[str] = []
    real = os.stat

    def counting(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            seen.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(stat_cache.os, "stat", counting)
    return seen


def test_unchanged_folder_costs_one_stat_and_no_rebuild(tmp_path: Path, stat_calls: List[str]) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    cache = DirListingCache()
    build = _Builds(tmp_path)
    assert cache.get(tmp_path, "k", build) == ["a.md"]
    stat_calls.clear()

    for _ in range(3):
        assert cache.get(tmp_path, "k", build) == ["a.md"]

    assert build.count == 1
    assert stat_calls == [str(tmp_path)] * 3


def test_unchanged_tree_costs_one_stat_per_folder(tmp_path: Path, stat_calls: List[str]) -> None:
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "sub" / "deep" / "x.md").write_text("x", encoding="utf-8")
    cache = DirListingCache()
    build = _Builds(tmp_path)
    cache.get(tmp_path, "k", build, recursive=True)
    stat_calls.clear()

    cache.get(tmp_path, "k", build, recursive=True)

    assert build.count == 1
    assert sorted(stat_calls) == sorted([str(tmp_path), str(tmp_path / "sub"), str(tmp_path / "sub" / "deep")])


def test_a_new_file_rebuilds(tmp_path: Path) -> None:
    cache = DirListingCache()
    build = _Builds(tmp_path)
    assert cache.get(tmp_path, "k", build) == []
    (tmp_path / "new.md").write_text("n", encoding="utf-8")
    assert cache.get(tmp_path, "k", build) == ["new.md"]
    assert build.count == 2


def test_a_new_file_in_a_nested_folder_rebuilds_a_recursive_listing(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    cache = DirListingCache()
    build = _Builds(tmp_path)
    cache.get(tmp_path, "k", build, recursive=True)
    (tmp_path / "sub" / "deep.md").write_text("d", encoding="utf-8")
    assert cache.get(tmp_path, "k", build, recursive=True) == ["deep.md"]
    assert build.count == 2


def test_a_removed_folder_rebuilds(tmp_path: Path) -> None:
    folder = tmp_path / "gone"
    folder.mkdir()
    cache = DirListingCache()
    build = _Builds(folder)
    (folder / "f.md").write_text("f", encoding="utf-8")
    assert cache.get(folder, "k", build) == ["f.md"]
    (folder / "f.md").unlink()
    folder.rmdir()
    assert cache.get(folder, "k", build) == []
    assert build.count == 2


def test_a_skipped_folder_is_not_part_of_the_fingerprint(tmp_path: Path, stat_calls: List[str]) -> None:
    (tmp_path / "node_modules").mkdir()
    cache = DirListingCache()
    build = _Builds(tmp_path)
    cache.get(tmp_path, "k", build, recursive=True, skip=frozenset({"node_modules"}))
    (tmp_path / "node_modules" / "pkg.js").write_text("p", encoding="utf-8")
    stat_calls.clear()

    cache.get(tmp_path, "k", build, recursive=True, skip=frozenset({"node_modules"}))

    assert build.count == 1
    assert stat_calls == [str(tmp_path)]


def test_keys_keep_listings_of_one_folder_apart(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    cache = DirListingCache()
    assert cache.get(tmp_path, "one", lambda: ["one"]) == ["one"]
    assert cache.get(tmp_path, "two", lambda: ["two"]) == ["two"]
    assert cache.get(tmp_path, "one", lambda: ["rebuilt"]) == ["one"]


def test_walk_files_reads_an_unchanged_tree_once_and_hands_out_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "skill"
    (skill / "memory").mkdir(parents=True)
    (skill / "SKILL.md").write_text("s", encoding="utf-8")
    (skill / "memory" / "obs.md").write_text("o", encoding="utf-8")
    monkeypatch.setattr(life_os_files, "_walk_cache", DirListingCache())
    reads: List[Path] = []
    real = life_os_files._walk_files

    def counting(root, life_os_root, category):
        reads.append(root)
        return real(root, life_os_root, category)

    monkeypatch.setattr(life_os_files, "_walk_files", counting)

    first = life_os_files.walk_files(skill, tmp_path.resolve(), None)
    first[0]["name"] = "mutated"
    second = life_os_files.walk_files(skill, tmp_path.resolve(), None)

    assert len(reads) == 1
    assert [f["name"] for f in second] != [f["name"] for f in first]
    assert {f["category"] for f in second} == {"memory", "skill"}

    (skill / "memory" / "new.md").write_text("n", encoding="utf-8")
    third = life_os_files.walk_files(skill, tmp_path.resolve(), None)
    assert len(reads) == 2
    assert "new.md" in {f["name"] for f in third}
