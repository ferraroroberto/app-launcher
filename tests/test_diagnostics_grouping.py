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
