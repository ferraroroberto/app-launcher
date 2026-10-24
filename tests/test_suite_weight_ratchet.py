"""The test-to-code ratchet the gate runs (scripts/suite_weight_ratchet.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import suite_weight_ratchet as ratchet
from src.subprocess_flags import NO_WINDOW


def _repo(tmp_path: Path, ceiling: str, files: dict[str, str]) -> Path:
    """A git repo with ``.fleet.toml`` at ``ceiling`` and ``files`` tracked or left untracked."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, capture_output=True, creationflags=NO_WINDOW)
    (repo / ".fleet.toml").write_text(f'[test_weight]\nratio_ceiling = "{ceiling}"\n', encoding="utf-8")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
    if (repo / "src").exists():
        subprocess.run(["git", "add", "src"], cwd=repo, check=True, capture_output=True, creationflags=NO_WINDOW)
    return repo


def _ceiling(repo: Path) -> str:
    return ratchet.read_ceiling((repo / ".fleet.toml").read_text(encoding="utf-8")).to_eng_string()


# Two product lines in src/; tests/ is left untracked, which still counts.
_FILES = {"src/a.py": "x = 1\ny = 2\n", "tests/test_a.py": "a\nb\nc\n", "docs/notes.md": "1\n2\n3\n4\n"}


def test_passes_at_or_under_the_ceiling(tmp_path: Path) -> None:
    assert ratchet.run(_repo(tmp_path, "1.5", _FILES), "check") == ratchet.EXIT_PASS


def test_fails_above_the_ceiling(tmp_path: Path) -> None:
    assert ratchet.run(_repo(tmp_path, "1.4999", _FILES), "check") == ratchet.EXIT_ABOVE


def test_update_lowers_the_ceiling_and_never_raises_it(tmp_path: Path) -> None:
    loose = _repo(tmp_path / "loose", "3", _FILES)
    assert ratchet.run(loose, "update") == ratchet.EXIT_PASS
    assert _ceiling(loose) == "1.5000"

    tight = _repo(tmp_path / "tight", "1", _FILES)
    assert ratchet.run(tight, "update") == ratchet.EXIT_ABOVE
    assert _ceiling(tight) == "1"


def test_raise_ceiling_is_the_explicit_way_up(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "1", _FILES)
    assert ratchet.run(repo, "raise") == ratchet.EXIT_PASS
    assert _ceiling(repo) == "1.5000"
    assert ratchet.run(repo, "check") == ratchet.EXIT_PASS


def test_a_count_that_cannot_be_established_is_not_confirmed(tmp_path: Path) -> None:
    no_product = _repo(tmp_path / "a", "9", {"tests/test_a.py": "a\n"})
    assert ratchet.run(no_product, "check") == ratchet.EXIT_NOT_CONFIRMED

    not_a_repo = tmp_path / "b"
    not_a_repo.mkdir()
    (not_a_repo / ".fleet.toml").write_text('[test_weight]\nratio_ceiling = "9"\n', encoding="utf-8")
    assert ratchet.run(not_a_repo, "check") == ratchet.EXIT_NOT_CONFIRMED


@pytest.mark.parametrize("table", ["", "[test_weight]\n", '[test_weight]\nratio_ceiling = 1.2\n',
                                   '[test_weight]\nratio_ceiling = "abc"\n'])
def test_a_missing_or_malformed_ceiling_is_a_config_error(tmp_path: Path, table: str) -> None:
    repo = _repo(tmp_path, "1", _FILES)
    (repo / ".fleet.toml").write_text(table, encoding="utf-8")
    assert ratchet.run(repo, "check") == ratchet.EXIT_CONFIG

