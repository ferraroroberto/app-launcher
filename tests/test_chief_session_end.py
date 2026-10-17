"""The session-host reaper publishes a chief ``session-end`` for hookless deaths (#1644).

Every test uses a tmp registry and a tmp inbox: the real
``~/.claude/hooks/state`` chief mailbox and ``chief-managed.json`` are never
touched (a stray event would wake the live chief).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import chief_session_end
from src.chief_session_end import publish_session_ends, resolve_paths

NOW = datetime(2026, 10, 17, 9, 30, 5, tzinfo=timezone.utc)
SID = "a" * 32
OTHER = "b" * 32


def _session(sid: str, agent: str = "claude") -> SimpleNamespace:
    return SimpleNamespace(session_id=sid, agent=agent)


def _registry(tmp_path: Path, rows: dict) -> Path:
    path = tmp_path / "chief-managed.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _events(inbox: Path) -> list:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(inbox.glob("*.json"))]


def test_managed_exit_publishes_exactly_one_v2_event(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        {SID: {"repo": "fleet-config", "number": 1455, "dispatch_id": "d" * 32}},
    )
    inbox = tmp_path / "chief-inbox"

    published = publish_session_ends([_session(SID)], registry=registry, inbox=inbox, now=NOW)

    assert len(published) == 1
    [event] = _events(inbox)
    assert event["v"] == 2
    assert event["kind"] == "session-end"
    assert event["event"] == "SessionEnd"
    assert event["source"] == "reaper"
    assert event["launcher_sid"] == SID
    assert event["repo"] == "fleet-config"
    assert event["number"] == 1455
    assert event["agent"] == "claude"
    assert event["dispatch_id"] == "d" * 32
    assert event["excerpt"] is None
    assert event["excerpt_source"] is None
    assert event["excerpt_unavailable"]
    assert event["ts"] == "2026-10-17T09:30:05Z"
    assert len(event["id"]) == 32 and int(event["id"], 16) >= 0
    # <published_ms>-<id>.json, id equal to the event's id.
    assert published[0].name == f"{int(NOW.timestamp() * 1000)}-{event['id']}.json"


def test_row_without_dispatch_id_yields_null(tmp_path: Path) -> None:
    registry = _registry(tmp_path, {SID: {"repo": "app-launcher", "number": 7}})
    inbox = tmp_path / "chief-inbox"
    publish_session_ends([_session(SID)], registry=registry, inbox=inbox, now=NOW)
    assert _events(inbox)[0]["dispatch_id"] is None


def test_only_the_managed_session_of_a_batch_publishes(tmp_path: Path) -> None:
    registry = _registry(tmp_path, {SID: {"repo": "r", "number": 1}})
    inbox = tmp_path / "chief-inbox"
    publish_session_ends([_session(OTHER), _session(SID)], registry=registry, inbox=inbox)
    assert [e["launcher_sid"] for e in _events(inbox)] == [SID]


def test_non_managed_exit_writes_nothing(tmp_path: Path) -> None:
    registry = _registry(tmp_path, {OTHER: {"repo": "r", "number": 1}})
    inbox = tmp_path / "chief-inbox"
    assert publish_session_ends([_session(SID)], registry=registry, inbox=inbox) == []
    assert not inbox.exists()


def test_missing_registry_is_a_determination_not_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox = tmp_path / "chief-inbox"
    with caplog.at_level(logging.WARNING):
        out = publish_session_ends(
            [_session(SID)], registry=tmp_path / "absent.json", inbox=inbox
        )
    assert out == []
    assert not inbox.exists()
    assert "undetermined" not in caplog.text


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '"a string"', "null"], ids=["corrupt", "list", "string", "null"]
)
def test_unreadable_registry_logs_undetermined_and_writes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    registry = tmp_path / "chief-managed.json"
    registry.write_text(content, encoding="utf-8")
    inbox = tmp_path / "chief-inbox"
    with caplog.at_level(logging.WARNING):
        out = publish_session_ends([_session(SID)], registry=registry, inbox=inbox)
    assert out == []
    assert not inbox.exists()
    assert "undetermined" in caplog.text


def test_registry_that_cannot_be_read_is_undetermined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A directory where the file should be: read_text raises OSError, not
    # FileNotFoundError, so it must not be mistaken for "nothing dispatched".
    registry = tmp_path / "chief-managed.json"
    registry.mkdir()
    inbox = tmp_path / "chief-inbox"
    with caplog.at_level(logging.WARNING):
        out = publish_session_ends([_session(SID)], registry=registry, inbox=inbox)
    assert out == []
    assert not inbox.exists()
    assert "undetermined" in caplog.text


def test_publish_is_atomic_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path, {SID: {"repo": "r", "number": 1}})
    inbox = tmp_path / "chief-inbox"
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        # At the instant of the rename the payload sits in a dot-tmp file and
        # no ``*.json`` exists yet, so a reader can never see a partial one.
        seen["src"] = Path(src).name
        seen["json_before"] = sorted(p.name for p in inbox.glob("*.json"))
        seen["tmp_complete"] = json.loads(Path(src).read_text(encoding="utf-8"))["kind"]
        return real_replace(src, dst)

    monkeypatch.setattr(chief_session_end.os, "replace", spy)
    publish_session_ends([_session(SID)], registry=registry, inbox=inbox)

    assert seen["src"].startswith(".event.") and seen["src"].endswith(".tmp")
    assert seen["json_before"] == []
    assert seen["tmp_complete"] == "session-end"
    assert [p.name for p in inbox.iterdir() if p.name.startswith(".")] == []
    assert len(list(inbox.glob("*.json"))) == 1


def test_failed_publish_leaves_neither_json_nor_temp_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path, {SID: {"repo": "r", "number": 1}})
    inbox = tmp_path / "chief-inbox"

    def boom(src, dst):
        raise PermissionError("held open")

    monkeypatch.setattr(chief_session_end.os, "replace", boom)
    assert publish_session_ends([_session(SID)], registry=registry, inbox=inbox) == []
    assert list(inbox.iterdir()) == []


def test_resolve_paths_reads_config_then_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "webapp_config.json"
    config.write_text(
        json.dumps({"chief_managed_file": str(tmp_path / "m.json"), "chief_inbox_dir": ""}),
        encoding="utf-8",
    )
    monkeypatch.setenv(chief_session_end.CONFIG_PATH_ENV, str(config))
    registry, inbox = resolve_paths()
    assert registry == tmp_path / "m.json"
    assert inbox == chief_session_end.DEFAULT_STATE_DIR / "chief-inbox"

    monkeypatch.setenv(chief_session_end.CONFIG_PATH_ENV, str(tmp_path / "absent.json"))
    assert resolve_paths() == (
        chief_session_end.DEFAULT_STATE_DIR / "chief-managed.json",
        chief_session_end.DEFAULT_STATE_DIR / "chief-inbox",
    )


def test_resolve_paths_matches_webapp_config() -> None:
    """The reaper's copy of the config location and defaults can't drift."""
    from src import webapp_config

    cfg = webapp_config.WebappConfig()
    assert chief_session_end.CONFIG_PATH_ENV == webapp_config.WEBAPP_CONFIG_PATH_ENV
    assert chief_session_end.DEFAULT_CONFIG_PATH == webapp_config.DEFAULT_CONFIG_PATH
    assert Path(cfg.chief_managed_file) == chief_session_end.DEFAULT_STATE_DIR / "chief-managed.json"
    assert Path(cfg.chief_inbox_dir) == chief_session_end.DEFAULT_STATE_DIR / "chief-inbox"
    assert chief_session_end.REGISTRY_KEY == "chief_managed_file"
    assert chief_session_end.INBOX_KEY == "chief_inbox_dir"


def test_reap_loop_hook_never_raises_and_uses_resolved_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.session_host import server

    registry = _registry(tmp_path, {SID: {"repo": "r", "number": 3}})
    inbox = tmp_path / "chief-inbox"
    monkeypatch.setattr(server, "resolve_paths", lambda: (registry, inbox))
    server._publish_chief_session_ends([_session(SID)])
    assert [e["number"] for e in _events(inbox)] == [3]

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "resolve_paths", broken)
    server._publish_chief_session_ends([_session(SID)])  # swallowed, logged


def test_reap_loop_publishes_for_what_the_reaper_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.session_host import server

    registry = _registry(tmp_path, {SID: {"repo": "r", "number": 9}})
    inbox = tmp_path / "chief-inbox"
    monkeypatch.setattr(server, "resolve_paths", lambda: (registry, inbox))
    monkeypatch.setattr(server, "_cleanup_ended_sessions", lambda sessions: None)
    monkeypatch.setattr(server.manager, "reap_dead", lambda: [_session(SID)])
    ticks = []

    async def one_tick(_seconds):
        ticks.append(1)
        if len(ticks) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(server.asyncio, "sleep", one_tick)
    asyncio.run(server._reap_loop())
    assert [e["number"] for e in _events(inbox)] == [9]
