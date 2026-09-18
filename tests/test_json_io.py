"""``atomic_write_json``'s temp-file contract (#1003).

The helper writes a sibling ``<name><suffix>.tmp`` and ``os.replace``s it
over the target, so a reader never sees a half-written file. It had no
direct tests at all, despite eleven modules writing their JSON through it.

The defect these pin: a write that failed **after** the temp file landed
left it behind. Only one of the eleven call sites cleaned that up
(``context_filter_state.write_mode``), and it did so by re-deriving this
helper's own private temp-file name — so every other site leaked, and the
one that didn't was coupled to an implementation detail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import _json_io
from src._json_io import atomic_write_json

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _tmp_siblings(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_writes_payload_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"mode": "on"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"mode": "on"}
    assert _tmp_siblings(tmp_path) == []


def _force_replace_failure(tmp_path: Path) -> Path:
    """A target the ``os.replace`` swap cannot overwrite.

    Replacing a non-empty directory with a file is refused *after* the temp
    file is already on disk — the same shape as a target held open by
    another process on Windows, which is the realistic trigger.
    """
    target = tmp_path / "state.json"
    target.mkdir()
    (target / "occupied").write_text("x", encoding="utf-8")
    return target


def test_failed_swap_leaves_no_orphaned_temp_file(tmp_path):
    """The leak itself. Proven by observing the stray file, not by reading
    the code: pre-fix this directory is left holding ``state.json.tmp``."""
    target = _force_replace_failure(tmp_path)
    with pytest.raises(OSError):
        atomic_write_json(target, {"mode": "on"})
    assert _tmp_siblings(tmp_path) == [], (
        "a failed swap left an orphaned temp file behind (#1003)"
    )


def test_cleanup_does_not_mask_the_original_error(tmp_path):
    """The ``finally`` must not swallow or replace the real failure — a
    silent success here would be worse than the leak."""
    target = _force_replace_failure(tmp_path)
    with pytest.raises(OSError):
        atomic_write_json(target, {"mode": "on"})


def test_unserializable_payload_never_creates_a_temp_file(tmp_path):
    """Boundary worth recording: ``json.dumps`` runs before ``write_text``
    opens anything, so a serialization failure leaks nothing either way.
    The leak closed above is specifically the post-write one."""
    target = tmp_path / "state.json"
    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": object()})
    assert _tmp_siblings(tmp_path) == []
    assert not target.exists()


def test_no_module_re_derives_the_private_temp_name():
    """Partial adoption is this helper's failure mode (#1003).

    The cleanup is only correct everywhere if every writer goes *through*
    the helper. A module that re-derives ``path.with_suffix(suffix +
    ".tmp")`` has coupled itself to a private detail and will drift — which
    is exactly how one call site ended up cleaning up and ten did not.
    """
    offenders = []
    for path in sorted(_REPO_ROOT.glob("src/*.py")) + sorted(
        _REPO_ROOT.glob("app/**/*.py")
    ):
        if path.name == "_json_io.py":
            continue
        text = path.read_text(encoding="utf-8")
        if '".tmp"' in text and "with_suffix" in text:
            offenders.append(path.relative_to(_REPO_ROOT).as_posix())
    assert not offenders, (
        "these re-derive atomic_write_json's private temp-file name instead "
        "of letting the helper own it: " + ", ".join(offenders)
    )
