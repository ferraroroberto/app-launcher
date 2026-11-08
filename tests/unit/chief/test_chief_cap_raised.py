"""The cap-raised chief mailbox event.

The inbox is always a ``tmp_path``: nothing here touches the real
``~/.claude/hooks/state/chief-inbox``. The canonical-instance fact is faked at
its seam.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp.chief_doorbell import count_pending
from src import chief_session_end, instance_role
from src.chief import cap_raised


def _cfg(inbox: Path, cap: int = 2, scope: str = "chief") -> SimpleNamespace:
    return SimpleNamespace(
        chief_worker_cap=cap, chief_worker_cap_scope=scope, chief_inbox_dir=str(inbox)
    )


def _events(inbox: Path):
    if not inbox.exists():
        return []
    return [json.loads(p.read_text("utf-8")) for p in sorted(inbox.glob("*.json"))]


@pytest.fixture
def canonical(monkeypatch):
    monkeypatch.setattr(
        instance_role, "canonical_instance", lambda *a, **k: (True, "canonical")
    )


class TestPublish:
    def test_a_raise_writes_exactly_one_event_with_the_contract_shape(
        self, tmp_path, canonical
    ):
        inbox = tmp_path / "chief-inbox"
        path = cap_raised.publish_cap_raised(_cfg(inbox, 2), _cfg(inbox, 3))

        (event,) = _events(inbox)
        stamp = event.pop("ts")
        assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert path is not None and path.name == f"{path.name.split('-')[0]}-{event['id']}.json"
        assert len(event["id"]) == 32
        assert {k: v for k, v in event.items() if k != "id"} == {
            "v": 2, "kind": "cap-raised", "event": "CapRaised", "old": 2, "new": 3,
            "launcher_sid": None, "repo": None, "number": None, "agent": None,
            "dispatch_id": None,
        }
        assert not list(inbox.glob(".*.tmp")), "the temp file is renamed away"

    def test_the_doorbell_counts_the_event_as_pending(self, tmp_path, canonical):
        inbox = tmp_path / "chief-inbox"
        assert count_pending(inbox, int(time.time() * 1000)) == 0
        cap_raised.publish_cap_raised(_cfg(inbox, 2), _cfg(inbox, 3))
        assert count_pending(inbox, int(time.time() * 1000)) == 1

    @pytest.mark.parametrize("old,new", [(3, 2), (3, 3)])
    def test_a_lowered_or_unchanged_cap_writes_nothing(
        self, tmp_path, canonical, old, new
    ):
        inbox = tmp_path / "chief-inbox"
        assert cap_raised.publish_cap_raised(_cfg(inbox, old), _cfg(inbox, new)) is None
        assert _events(inbox) == []

    def test_a_scope_switch_all_to_chief_writes_old_equal_new(self, tmp_path, canonical):
        inbox = tmp_path / "chief-inbox"
        cap_raised.publish_cap_raised(_cfg(inbox, 3, "all"), _cfg(inbox, 3, "chief"))
        (event,) = _events(inbox)
        assert (event["old"], event["new"]) == (3, 3)

    def test_a_scope_switch_chief_to_all_writes_nothing(self, tmp_path, canonical):
        inbox = tmp_path / "chief-inbox"
        cap_raised.publish_cap_raised(_cfg(inbox, 3, "chief"), _cfg(inbox, 3, "all"))
        assert _events(inbox) == []

    def test_a_failed_write_is_swallowed(self, tmp_path, canonical, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(cap_raised, "publish_event", boom)
        inbox = tmp_path / "chief-inbox"
        assert cap_raised.publish_cap_raised(_cfg(inbox, 2), _cfg(inbox, 4)) is None

    def test_a_non_canonical_instance_never_writes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            instance_role, "canonical_instance", lambda *a, **k: (False, "linked-worktree")
        )
        inbox = tmp_path / "chief-inbox"
        assert cap_raised.publish_cap_raised(_cfg(inbox, 2), _cfg(inbox, 4)) is None
        assert _events(inbox) == []


def test_the_event_version_is_the_mailbox_wire_version():
    assert cap_raised.build_event(1, 2)["v"] == chief_session_end.EVENT_VERSION == 2
