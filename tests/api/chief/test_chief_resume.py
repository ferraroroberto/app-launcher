"""Chief resume: the conversation worth reattaching, and the remembered pointer that finds it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.webapp.routers import board_chief as chief_router
from src.chief import pointer as chief_pointer

from tests.api.chief._support import (
    _chief,
    _chief_row,
    _chief_state_row,
    _recent_iso,
    _transcript,
    _write_pointer,
)


# Each case: state rows, an optional durable pointer ``(sid, transcript kind,
# age)``, the live chief's ``preferred_sid``, and the conversation that must
# be reattached ("" = fall back to a fresh spawn). Substance decides before
# recency: a typed conversation beats a newer bootstrap-only one, "unknown"
# ranks below "confirmed" but is never a negative, and the pointer joins the
# ranking without jumping it.
_RANKER_CASES = [
    pytest.param({}, None, "", "", id="no-rows"),
    pytest.param(
        {"old": _chief(hours=2), "new": _chief(hours=1)},
        None, "", "new", id="newest-of-two-unknown",
    ),
    pytest.param(
        {"x": _chief(hours=1, cwd="E:/automation/app-launcher")},
        None, "", "", id="wrong-project-excluded",
    ),
    pytest.param(
        {"x": _chief(hours=1, name="fleet-config")},
        None, "", "", id="wrong-name-excluded",
    ),
    pytest.param(
        {"x": _chief(hours=1, name="Chief")},
        None, "", "x", id="name-match-case-insensitive",
    ),
    pytest.param(
        {"x": _chief(hours=30)}, None, "", "", id="row-past-24h-excluded",
    ),
    pytest.param(
        {"x": _chief(hours=1, cwd="E:/automation/app-launcher-wt-42",
                     project="fleet-config")},
        None, "", "x", id="project-field-beats-cwd-basename",
    ),
    pytest.param({"x": "not-a-row"}, None, "", "", id="non-dict-row-skipped"),
    pytest.param(
        {"real": _chief(hours=3, transcript="typed"), "blank": _chief(minutes=1, transcript="blank")},
        None, "", "real", id="bootstrap-only-newest-loses-to-substantive-older",
    ),
    pytest.param(
        {"old": _chief(hours=3, transcript="typed"), "new": _chief(minutes=5, transcript="typed")},
        None, "", "new", id="newest-wins-between-substantive",
    ),
    pytest.param(
        {"known": _chief(hours=3, transcript="typed"),
         "unknown": _chief(minutes=5, transcript="missing")},
        None, "", "known", id="confirmed-outranks-unknown",
    ),
    pytest.param(
        {"x": _chief(hours=1, transcript="missing")}, None, "", "x",
        id="unknown-resumable-when-nothing-better",
    ),
    pytest.param(
        {"blank": _chief(minutes=1, transcript="blank")}, None, "", "",
        id="only-bootstrap-resumes-nothing",
    ),
    pytest.param(
        {"hand": _chief(minutes=2, transcript="typed", name="fleet-config-79"),
         "other": _chief(hours=1, transcript="typed")},
        None, "hand", "hand", id="preferred-wins-with-project-derived-name",
    ),
    pytest.param(
        {"blank": _chief(minutes=1, transcript="blank"), "real": _chief(hours=3, transcript="typed")},
        None, "blank", "real", id="preferred-ignored-when-bootstrap-only",
    ),
    pytest.param(
        {"x": _chief(minutes=1, transcript="typed", name="app-launcher-11",
                     cwd="E:/automation/app-launcher")},
        None, "x", "", id="preferred-outside-fleet-config-ignored",
    ),
    pytest.param(
        {"real": _chief(hours=3, transcript="typed")}, None, "ghost", "real",
        id="unknown-preferred-falls-through-to-scan",
    ),
    pytest.param(
        {"hand": _chief(minutes=20, transcript="typed", name="fleet-config-79")},
        ("hand", "row", None), "", "hand", id="pointer-finds-hand-started-chief",
    ),
    pytest.param(
        {}, ("pruned", "typed", timedelta(days=3)), "", "pruned",
        id="pointer-finds-conversation-past-24h-prune",
    ),
    pytest.param(
        {"fresh": _chief(minutes=5, transcript="typed")},
        ("pointed", "typed", timedelta(days=2)), "", "fresh",
        id="newer-substantive-row-outranks-pointer",
    ),
    pytest.param(
        {"hand": _chief(minutes=2, transcript="typed", name="fleet-config-79"),
         "older": _chief(hours=6, transcript="typed")},
        ("hand", "row", timedelta(days=3)), "", "hand",
        id="pointer-ranked-by-its-row-when-row-exists",
    ),
    pytest.param(
        {}, ("blank", "blank", None), "", "", id="bootstrap-only-pointer-never-resumed",
    ),
    pytest.param(
        {"live": _chief(minutes=1, transcript="typed", name="fleet-config-79")},
        ("pointed", "typed", None), "live", "live",
        id="live-preferred-beats-pointer",
    ),
    pytest.param(
        {}, ("old", "typed", timedelta(days=8)), "", "", id="expired-pointer-ignored",
    ),
    pytest.param(
        {}, ("gone", "gone", None), "", "", id="pointer-at-deleted-transcript-ignored",
    ),
    pytest.param(
        {"real": _chief(hours=2, transcript="typed")}, ("x", "corrupt", None), "", "real",
        id="corrupt-pointer-degrades-to-scan",
    ),
    pytest.param(
        {"same": _chief(minutes=5, transcript="typed")}, ("same", "row", None), "", "same",
        id="pointer-naming-a-scanned-row-is-harmless",
    ),
]


@pytest.mark.parametrize(("specs", "pointer", "preferred", "expected"), _RANKER_CASES)
def test_resume_reattaches_the_conversation_worth_resuming(
    tmp_path, specs, pointer, preferred, expected
):
    rows = {}
    for sid, spec in specs.items():
        if not isinstance(spec, dict):
            rows[sid] = spec
            continue
        extra = {k: v for k, v in spec.items() if k not in ("age", "transcript")}
        kind = spec["transcript"]
        if kind in ("typed", "blank"):
            extra["transcript_path"] = _transcript(tmp_path, sid, typed=kind == "typed")
        elif kind == "missing":
            extra["transcript_path"] = "C:/nope/missing.jsonl"
        rows[sid] = _chief_state_row(f"{sid}-sess", _recent_iso(**spec["age"]), **extra)
    if pointer:
        sid, kind, age = pointer
        if kind == "corrupt":
            chief_pointer.CHIEF_POINTER_FILE.write_text("{corrupt", encoding="utf-8")
        else:
            transcript = (
                rows[sid]["transcript_path"] if kind == "row"
                else _transcript(tmp_path, sid, typed=kind != "blank")
            )
            _write_pointer(sid, transcript, age=age)
            if kind == "gone":
                Path(transcript).unlink()
    assert (
        chief_router._find_resumable_chief_session_id(rows, preferred_sid=preferred)
        == expected
    )


class TestEnsureResume:
    """Integration coverage for ``resume`` on ``POST /api/board/chief/ensure``
    (#633): reattach the most recent chief conversation via a direct
    ``claude --resume <id>`` instead of starting fresh, falling back cleanly
    when nothing is resumable."""

    def test_no_resumable_conversation_falls_back_to_fresh_and_types_chief(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, _, overrides = webapp_client
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["spawned"] is True
        assert body["resumed"] is False
        assert body["resume_fallback_reason"]
        # Fallback is indistinguishable from a plain fresh spawn: /chief is
        # still typed, and no --resume token rides the flags.
        assert "--resume" not in _spawn["flags"]
        assert overrides["session"].send_input.call_args_list[0].args == (
            8446, "chief-1", "/chief", True,
        )

    def test_resumable_conversation_spawns_direct_resume_and_skips_chief_type(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, app, overrides = webapp_client
        cfg = app.state.webapp_config
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "old-chief-sess": _chief_state_row(
                    "chief-1", _recent_iso(minutes=5),
                ),
            }),
            encoding="utf-8",
        )
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["spawned"] is True
        assert body["resumed"] is True
        assert body["resume_fallback_reason"] == ""
        assert "--resume old-chief-sess" in _spawn["flags"]
        assert _spawn["label"] == "chief"
        # The conversation is already past its own boot — no /chief re-typed.
        overrides["session"].send_input.assert_not_called()

    def test_restart_resume_stops_live_chief_first_then_reattaches_it(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        """The Resume button (``resume`` + ``restart``) repurposes the same
        stop-first ordering ``fresh`` uses (#633) — deliberately: the chief
        being stopped is exactly the conversation the state row (fresh off
        its own hook write) points back to, so this is a context-preserving
        restart, not an accident of shared plumbing."""
        client, app, overrides = webapp_client
        cfg = app.state.webapp_config
        overrides["session"].list_sessions.return_value = [_chief_row()]
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "chief-old": _chief_state_row(
                    "chief-old", _recent_iso(minutes=5),
                ),
            }),
            encoding="utf-8",
        )
        probes = iter([{"alive": False}])

        def _get_session(port, sid):
            try:
                return next(probes)
            except StopIteration:
                return {"alive": True, "output_chars": 64}

        overrides["session"].get_session.side_effect = _get_session
        resp = client.post(
            "/api/board/chief/ensure", json={"resume": True, "restart": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["resumed"] is True
        assert overrides["session"].stop.call_args_list[0].args == (
            8446, "chief-old", "quit"
        )
        assert "--resume chief-old" in _spawn["flags"]

    @pytest.mark.parametrize("payload", [
        pytest.param({"resume": True}, id="pre-fix-page"),
        pytest.param({"resume": True, "if_absent": True}, id="1353-page"),
    ])
    def test_resume_alone_keeps_a_live_chief(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, payload,
    ):
        """#1351: ``resume`` without an explicit restart intent must keep a
        live chief: no stop, no spawn, the same session id back so the text
        lands in the conversation that asked. A message send (chat bar, Board
        answer sheet) ensures this way — and so does every Board page loaded
        before the fix, which sends ``resume`` alone and is never reloaded on
        a phone. Before the fix that shape stop-and-resumed the live chief
        (twice in real use after #1353 had shipped)."""
        client, app, overrides = webapp_client
        cfg = app.state.webapp_config
        overrides["session"].list_sessions.return_value = [_chief_row()]
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "chief-conv": _chief_state_row("chief-old", _recent_iso(minutes=5)),
            }),
            encoding="utf-8",
        )
        # Probes a respawn would need, so a regression fails on the
        # assertions below rather than on an unmocked wait.
        probes = iter([{"alive": False}])

        def _get_session(port, sid):
            try:
                return next(probes)
            except StopIteration:
                return {"alive": True, "output_chars": 64}

        overrides["session"].get_session.side_effect = _get_session
        resp = client.post("/api/board/chief/ensure", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "chief-old"
        assert body["spawned"] is False
        overrides["session"].stop.assert_not_called()
        assert _spawn == {}, "a live chief must not be respawned"

    def test_restart_button_shape_stops_and_resumes_a_live_chief(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir,
    ):
        """``fresh`` (the Restart button sends ``fresh`` + ``resume``, #649)
        is an explicit restart intent on its own — no ``restart`` needed."""
        client, _app, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_chief_row()]
        probes = iter([{"alive": False}])

        def _get_session(port, sid):
            try:
                return next(probes)
            except StopIteration:
                return {"alive": True, "output_chars": 64}

        overrides["session"].get_session.side_effect = _get_session
        resp = client.post(
            "/api/board/chief/ensure", json={"fresh": True, "resume": True},
        )
        assert resp.status_code == 200
        assert resp.json()["spawned"] is True
        assert overrides["session"].stop.call_args_list[0].args == (
            8446, "chief-old", "quit"
        )

    def test_resume_skips_the_live_blank_chief_for_the_real_conversation(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, tmp_path,
    ):
        """End-to-end shape of the #670 incident: the chief alive right now is
        the blank one a fallback spawned after a deploy killed the real PTY.
        Stop-first preference must not hand it back — the conversation it
        displaced is the one to reattach."""
        client, app, overrides = webapp_client
        cfg = app.state.webapp_config
        overrides["session"].list_sessions.return_value = [_chief_row()]
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "blank-conv": _chief_state_row(
                    "chief-old", _recent_iso(minutes=1),
                    transcript_path=_transcript(tmp_path, "blank", typed=False),
                ),
                "real-conv": _chief_state_row(
                    "dead-sess", _recent_iso(hours=3),
                    transcript_path=_transcript(tmp_path, "real", typed=True),
                ),
            }),
            encoding="utf-8",
        )
        probes = iter([{"alive": False}])

        def _get_session(port, sid):
            try:
                return next(probes)
            except StopIteration:
                return {"alive": True, "output_chars": 64}

        overrides["session"].get_session.side_effect = _get_session
        resp = client.post(
            "/api/board/chief/ensure", json={"resume": True, "restart": True},
        )
        assert resp.status_code == 200
        assert resp.json()["resumed"] is True
        assert "--resume real-conv" in _spawn["flags"]
        assert "blank-conv" not in _spawn["flags"]

    def test_resume_honors_chief_model(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session,
    ):
        client, app, overrides = webapp_client
        app.state.webapp_config.chief_model = "opus"
        cfg = app.state.webapp_config
        Path(cfg.sessions_state_file).write_text(
            json.dumps({
                "hook-row-1": _chief_state_row(
                    "old-chief-sess", "2026-07-27T07:14:05Z",
                ),
            }),
            encoding="utf-8",
        )
        client.post("/api/board/chief/ensure", json={"resume": True})
        assert "--model opus" in _spawn["flags"]


class TestNoteChiefConversation:
    """#675's write half: the pointer is recorded while the chief is alive —
    that is the only moment both shapes above are knowable — without putting
    file IO on ``GET /api/board``'s 5s poll."""

    async def _drain(self):
        """Await the fire-and-forget write task the noter schedules."""
        import asyncio
        pending = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        if pending:
            await asyncio.gather(*pending)

    async def test_live_chief_is_recorded_with_its_transcript(self, tmp_path):
        transcript = _transcript(tmp_path, "live", typed=True)
        rows = {
            "live-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1), transcript_path=transcript,
            ),
        }
        chief_router._note_chief_conversation([_chief_row()], rows)
        await self._drain()
        pointer = chief_pointer.read_chief_pointer()
        assert pointer["session_id"] == "live-conv"
        assert pointer["transcript_path"] == transcript
        assert pointer["source"] == "live"

    async def test_self_healed_chief_is_recorded_too(self, tmp_path):
        """The case the pointer exists for: a chief the launcher never spawned
        carries no ``label`` of its own until ``_reconcile_chief_label`` heals
        it, and its row is named after the project."""
        transcript = _transcript(tmp_path, "hand", typed=True)
        rows = {
            "hand-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1),
                name="fleet-config-79", transcript_path=transcript,
            ),
        }
        live = chief_router._reconcile_chief_labels(
            [_chief_row(
                label="", name="fleet-config", live_title="✳ chief",
                project_dir="E:/automation/fleet-config",
            )],
            rows,
        )
        chief_router._note_chief_conversation(live, rows)
        await self._drain()
        assert chief_pointer.read_chief_pointer()["session_id"] == "hand-conv"

    async def test_unchanged_chief_writes_nothing_on_the_next_poll(
        self, tmp_path, monkeypatch
    ):
        """The poll-budget contract: the memo means a steady-state
        ``GET /api/board`` does no pointer IO at all."""
        rows = {
            "live-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1),
                transcript_path=_transcript(tmp_path, "live", typed=True),
            ),
        }
        writes = []
        monkeypatch.setattr(
            chief_pointer, "write_chief_pointer",
            lambda *a, **k: writes.append(a[0]),
        )
        for _ in range(3):
            chief_router._note_chief_conversation([_chief_row()], rows)
            await self._drain()
        assert writes == ["live-conv"]

    async def test_a_different_chief_conversation_rewrites_the_pointer(
        self, tmp_path, monkeypatch
    ):
        writes = []
        monkeypatch.setattr(
            chief_pointer, "write_chief_pointer",
            lambda *a, **k: writes.append(a[0]),
        )
        for conv in ("first-conv", "second-conv"):
            rows = {
                conv: _chief_state_row(
                    "chief-old", _recent_iso(minutes=1),
                    transcript_path=_transcript(tmp_path, conv, typed=True),
                ),
            }
            chief_router._note_chief_conversation([_chief_row()], rows)
            await self._drain()
        assert writes == ["first-conv", "second-conv"]

    async def test_no_live_chief_records_nothing(self, tmp_path):
        rows = {
            "some-conv": _chief_state_row(
                "other-sess", _recent_iso(minutes=1),
                transcript_path=_transcript(tmp_path, "some", typed=True),
            ),
        }
        chief_router._note_chief_conversation(
            [_chief_row(label="", name="fleet-config", alive=True)], rows
        )
        await self._drain()
        assert chief_pointer.read_chief_pointer() == {}

    async def test_row_without_a_transcript_records_nothing(self):
        """A pointer that can't be substance-checked or resumed is worth less
        than no pointer."""
        rows = {"live-conv": _chief_state_row("chief-old", _recent_iso(minutes=1))}
        chief_router._note_chief_conversation([_chief_row()], rows)
        await self._drain()
        assert chief_pointer.read_chief_pointer() == {}


class TestEnsureRefreshesThePointer:

    def test_resume_restamps_the_pointer_it_reattached(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session, tmp_path,
    ):
        """A conversation past the row prune has no row to re-derive from, so
        pressing Resume is the moment that restarts its 7-day clock."""
        client, _, _ = webapp_client
        transcript = _transcript(tmp_path, "pruned", typed=True)
        _write_pointer("pruned-conv", transcript, age=timedelta(days=6))
        resp = client.post("/api/board/chief/ensure", json={"resume": True})
        assert resp.status_code == 200
        assert resp.json()["resumed"] is True
        assert "--resume pruned-conv" in _spawn["flags"]
        pointer = chief_pointer.read_chief_pointer()
        assert pointer["session_id"] == "pruned-conv"
        assert pointer["source"] == "ensure-resume"
        # Restamped: it was 6 days old going in, and would expire in one more.
        assert (
            datetime.now(timezone.utc)
            - chief_router.board.parse_iso(pointer["seen_at"])
        ) < timedelta(minutes=5)

    def test_fresh_spawn_does_not_touch_the_pointer(
        self, webapp_client, _bypass_gate, _fast_probe, _spawn,
        _fleet_config_dir, _ready_session, tmp_path,
    ):
        """Nothing was reattached, so nothing is known yet — the brand-new
        conversation's id only exists once a hook fires, and the poll-side
        noter records it then."""
        client, _, _ = webapp_client
        _write_pointer(
            "blank-conv", _transcript(tmp_path, "blank", typed=False)
        )
        resp = client.post("/api/board/chief/ensure", json={})
        assert resp.status_code == 200
        assert chief_pointer.read_chief_pointer().get("session_id") == (
            "blank-conv"
        )
