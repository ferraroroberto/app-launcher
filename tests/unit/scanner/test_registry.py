"""``src.registry.load_registry`` strict-mode read-failure signaling (#925).

A missing ``config/apps.json`` legitimately means "empty registry" for every
caller. A file that exists but fails to parse/read is different — most
callers (Apps-tab scan/save, the CLI scan command) still tolerate that as an
empty registry unchanged, but ``app.tray.registered_trays.launch_all()``
needs to tell it apart from "nothing configured" so it can retry a transient
boot-time glitch instead of silently no-op'ing. ``strict=True`` is the knob
that lets it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.registry import RegistryReadError, load_registry


def test_missing_file_returns_empty_registry(tmp_path: Path):
    registry = load_registry(tmp_path / "does-not-exist.json")
    assert registry.apps == []


def test_missing_file_returns_empty_registry_even_when_strict(tmp_path: Path):
    """A missing file is never treated as a read failure — strict mode only
    changes behavior for a file that exists but can't be parsed."""
    registry = load_registry(tmp_path / "does-not-exist.json", strict=True)
    assert registry.apps == []


def test_corrupt_file_returns_empty_registry_by_default(tmp_path: Path):
    path = tmp_path / "apps.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    registry = load_registry(path)
    assert registry.apps == []


def test_corrupt_file_raises_in_strict_mode(tmp_path: Path):
    path = tmp_path / "apps.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    with pytest.raises(RegistryReadError):
        load_registry(path, strict=True)
