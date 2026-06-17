"""Unit tests for port-listener parent grouping (#224).

``_assign_parents`` decides which listeners are helper services of another
listener, by process ancestry. Driven with a fake ``ppid_lookup`` so it's
deterministic and needs no real processes.
"""

from __future__ import annotations

from typing import Dict, Optional

from src.diagnostics import PortOwner, _assign_parents


def _lookup(ppids: Dict[int, int]):
    return lambda pid: ppids.get(pid)


def test_descendant_grouped_under_ancestor_listener() -> None:
    # hub (:8000) -> stub 150 (not a listener) -> tts child (:8093)
    hub = PortOwner(pid=100, port=8000)
    tts = PortOwner(pid=200, port=8093)
    _assign_parents([hub, tts], _lookup({200: 150, 150: 100, 100: 1}))
    assert tts.parent_pid == 100  # nested under the hub
    assert hub.parent_pid is None  # top-level


def test_sibling_apps_not_grouped() -> None:
    # Two apps in separate process trees must never group together.
    a = PortOwner(pid=100, port=8000)
    b = PortOwner(pid=300, port=8443)
    _assign_parents([a, b], _lookup({100: 1, 300: 250, 250: 1}))
    assert a.parent_pid is None
    assert b.parent_pid is None


def test_nearest_listener_ancestor_wins() -> None:
    # grandchild -> child(listener) -> root(listener): pick the nearer one.
    root = PortOwner(pid=100, port=8000)
    child = PortOwner(pid=200, port=8093)
    grandchild = PortOwner(pid=300, port=8094)
    _assign_parents(
        [root, child, grandchild],
        _lookup({300: 200, 200: 100, 100: 1}),
    )
    assert grandchild.parent_pid == 200
    assert child.parent_pid == 100
    assert root.parent_pid is None


def test_cycle_terminates() -> None:
    # Pathological parent cycle must not spin forever.
    a = PortOwner(pid=100, port=8000)
    b = PortOwner(pid=200, port=8093)
    _assign_parents([a, b], _lookup({100: 200, 200: 100}))
    assert a.parent_pid == 200
    assert b.parent_pid == 100


def test_unknown_parent_left_top_level() -> None:
    a = PortOwner(pid=100, port=8000)
    _assign_parents([a], lambda pid: None)
    assert a.parent_pid is None


# --- shared-cwd fallback for detached helpers (#243) ------------------------


def test_detached_helpers_grouped_by_cwd() -> None:
    # local-llm-hub spawns its TTS + translate helpers detached, so their
    # ppids don't reach the hub. Same cwd → nest under the lowest-port one.
    hub = PortOwner(pid=100, port=8000, cwd=r"E:\automation\local-llm-hub")
    tts = PortOwner(pid=200, port=8093, cwd=r"E:\automation\local-llm-hub")
    xlate = PortOwner(pid=300, port=8094, cwd=r"E:\automation\local-llm-hub")
    # No ancestry link between any of them.
    _assign_parents([hub, tts, xlate], _lookup({100: 1, 200: 1, 300: 1}))
    assert hub.parent_pid is None  # lowest port → the parent
    assert tts.parent_pid == 100
    assert xlate.parent_pid == 100


def test_cwd_fallback_ignores_empty_cwd() -> None:
    # Listeners whose cwd couldn't be read must never collapse together.
    a = PortOwner(pid=100, port=8000, cwd="")
    b = PortOwner(pid=200, port=8093, cwd="")
    _assign_parents([a, b], _lookup({100: 1, 200: 1}))
    assert a.parent_pid is None
    assert b.parent_pid is None


def test_cwd_fallback_separate_dirs_not_grouped() -> None:
    # Different working directories → genuinely separate apps, no nesting.
    a = PortOwner(pid=100, port=8000, cwd=r"E:\automation\local-llm-hub")
    b = PortOwner(pid=200, port=8443, cwd=r"E:\automation\voice-transcriber")
    _assign_parents([a, b], _lookup({100: 1, 200: 1}))
    assert a.parent_pid is None
    assert b.parent_pid is None


def test_cwd_normalized_case_and_separators() -> None:
    # Windows: case-insensitive, separator-insensitive cwd comparison.
    hub = PortOwner(pid=100, port=8000, cwd=r"E:\automation\local-llm-hub")
    tts = PortOwner(pid=200, port=8093, cwd="e:/automation/local-llm-hub/")
    _assign_parents([hub, tts], _lookup({100: 1, 200: 1}))
    assert tts.parent_pid == 100


def test_ancestry_wins_over_cwd_fallback() -> None:
    # A helper already nested by ancestry isn't re-pointed by the cwd pass,
    # even if it shares a directory with a different lower-port listener.
    hub = PortOwner(pid=100, port=8000, cwd=r"E:\automation\local-llm-hub")
    mid = PortOwner(pid=150, port=8050, cwd=r"E:\automation\local-llm-hub")
    child = PortOwner(pid=200, port=8093, cwd=r"E:\automation\local-llm-hub")
    # child is a real PID descendant of mid (mid -> child).
    _assign_parents(
        [hub, mid, child], _lookup({100: 1, 150: 1, 200: 150})
    )
    assert child.parent_pid == 150  # ancestry kept, not re-pointed to hub
    assert mid.parent_pid == 100  # mid still nests under hub by cwd
    assert hub.parent_pid is None
