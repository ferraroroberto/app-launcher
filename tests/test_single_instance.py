"""Unit tests for the canonical named-mutex primitive (app/tray/single_instance.py).

The cross-*process* guarantee is Windows-only and proven in the field
(project-scaffolding#39); these tests cover the importable contract and the
in-process behaviour on whatever platform runs them.
"""

from __future__ import annotations

import sys
import uuid

import pytest

from app.tray.single_instance import SingleInstance, cross_process_lock

_WINDOWS = sys.platform == "win32"


def _unique(prefix: str) -> str:
    """A mutex name unlikely to collide with anything real on the box."""
    return f"Local\\scaffold-test-{prefix}-{uuid.uuid4().hex}"


def test_single_instance_acquires_when_free() -> None:
    inst = SingleInstance(_unique("free"))
    try:
        assert inst.acquired is True
    finally:
        inst.release()


def test_release_is_idempotent() -> None:
    inst = SingleInstance(_unique("idem"))
    inst.release()
    inst.release()  # must not raise on a second call
    assert inst._handle is None


def test_context_manager_releases() -> None:
    name = _unique("ctx")
    with SingleInstance(name) as inst:
        assert inst.acquired is True
    assert inst._handle is None


@pytest.mark.skipif(not _WINDOWS, reason="cross-process mutex semantics are Windows-only")
def test_duplicate_is_rejected_then_freed() -> None:
    name = _unique("dup")
    first = SingleInstance(name)
    try:
        assert first.acquired is True
        second = SingleInstance(name)  # same name, live sibling holds it
        assert second.acquired is False
        assert second._handle is None  # the duplicate stood down cleanly
    finally:
        first.release()
    # Once the holder releases, the name is free again.
    third = SingleInstance(name)
    try:
        assert third.acquired is True
    finally:
        third.release()


def test_cross_process_lock_yields_held() -> None:
    with cross_process_lock(_unique("lock"), timeout_s=2.0) as held:
        assert held is True


def test_cross_process_lock_reentrant_same_thread() -> None:
    # A Windows mutex is owned per-thread and recursive, so a nested acquire on
    # the same name from the same thread must not deadlock.
    name = _unique("reentrant")
    with cross_process_lock(name, timeout_s=2.0) as outer:
        assert outer is True
        with cross_process_lock(name, timeout_s=2.0) as inner:
            assert inner is True
