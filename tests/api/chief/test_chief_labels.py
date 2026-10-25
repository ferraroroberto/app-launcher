"""The chief's identity: label reconciliation and the endpoints' passkey gate."""

from __future__ import annotations



from app.webapp.routers import board_chief as chief_router
from src import chief_pointer

from tests.api.chief._support import _chief_row, _chief_state_row, _recent_iso, _transcript


class TestReconcileChiefLabel:
    """Unit coverage for ``_reconcile_chief_label`` (#617, extended #628): a
    chief PTY started outside ``ensure`` self-heals its ``label`` from any of
    three independent signals — ``prompt_title`` (#266, a fresh ``/chief``
    typed into a brand-new PTY), ``shared_name`` (fleet-config#302, Claude's
    own persisted conversation identity, which survives a Resume into a new
    PTY after a session-host restart — the case actually observed live
    against real session 1c8e6dde…, where the first line submitted into the
    new PTY was Roberto's own chat message, not ``/chief``), or ``live_title``
    (#628, the OSC title session_host parses straight off the PTY's own
    output — available before either of the other two, since it needs no
    hook and no submitted prompt at all)."""

    def _unlabelled_row(self, **extra):
        row = {
            "session_id": "manual-chief",
            "kind": "pty",
            "label": "",
            "prompt_title": "",
            "project_dir": "E:/automation/fleet-config",
            "alive": True,
        }
        row.update(extra)
        return row

    def test_unlabelled_chief_prompt_in_fleet_config_gets_healed(self):
        row = self._unlabelled_row(prompt_title="/chief")
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_resumed_chief_healed_via_shared_name_despite_wrong_prompt(self):
        """The live-observed case: prompt_title is whatever the human typed
        first into the resumed PTY, never "/chief" — only shared_name (from
        Claude's own persisted identity) carries the signal."""
        row = self._unlabelled_row(prompt_title="ok restarted, check all is good")
        healed = chief_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == "chief"

    def test_already_labelled_row_is_untouched(self):
        row = self._unlabelled_row(label="chief", prompt_title="whatever")
        assert chief_router._reconcile_chief_label(row, shared_name=None) is row

    def test_wrong_prompt_title_and_shared_name_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="/chief please help")
        healed = chief_router._reconcile_chief_label(row, shared_name="fleet-config")
        assert healed["label"] == ""

    def test_wrong_project_dir_is_not_healed(self):
        row = self._unlabelled_row(
            prompt_title="/chief", project_dir="E:/automation/app-launcher"
        )
        healed = chief_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == ""

    def test_remote_kind_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="/chief", kind="remote")
        healed = chief_router._reconcile_chief_label(row, shared_name="chief")
        assert healed["label"] == ""

    def test_shared_name_match_is_case_insensitive(self):
        row = self._unlabelled_row(prompt_title="hi")
        healed = chief_router._reconcile_chief_label(row, shared_name="Chief")
        assert healed["label"] == "chief"

    def test_source_dict_is_never_mutated(self):
        row = self._unlabelled_row(prompt_title="/chief")
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed is not row
        assert row["label"] == ""

    def test_resumed_chief_healed_via_live_title_before_hook_or_prompt(self):
        """#628, reproduced from the real resumed chief session (7174c1d2…):
        right after a host reboot, prompt_title is whatever was typed first
        into the new PTY (not "/chief") and shared_name hasn't caught up yet
        (no hook has fired) — but live_title, parsed straight off Claude
        Code's own OSC title re-emitted on Resume, already names the
        conversation. This is the exact "undetectable until its first hook
        fires" window #628 was filed over."""
        row = self._unlabelled_row(
            prompt_title="can I compact now?", live_title="👑 chief"
        )
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_live_title_match_is_case_insensitive_and_tolerates_prefix(self):
        row = self._unlabelled_row(prompt_title="hi", live_title="🔥 CHIEF")
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == "chief"

    def test_unrelated_live_title_is_not_healed(self):
        row = self._unlabelled_row(prompt_title="hi", live_title="fleet-config")
        healed = chief_router._reconcile_chief_label(row, shared_name=None)
        assert healed["label"] == ""


class TestReconcileChiefLabels:
    """``_reconcile_chief_labels`` (#617): the batch join — pulls
    ``shared_name`` from the state rows via the same agent-aware claim walk
    every other cross-tab title uses, then applies the per-session heal."""

    def test_joins_shared_name_from_state_row_by_launcher_session_id(self):
        live = [{
            "session_id": "manual-chief", "kind": "pty", "label": "",
            "prompt_title": "ok restarted, check all is good",
            "project_dir": "E:/automation/fleet-config", "alive": True,
            "started_at": "2026-07-27T07:14:00Z",
        }]
        state_rows = {
            "hook-row-1": {
                "launcher_session_id": "manual-chief", "agent": "claude",
                "name": "chief", "updated_at": "2026-07-27T07:14:05Z",
            },
        }
        healed = chief_router._reconcile_chief_labels(live, state_rows)
        assert healed[0]["label"] == "chief"

    def test_no_matching_state_row_leaves_label_empty(self):
        live = [{
            "session_id": "manual-chief", "kind": "pty", "label": "",
            "prompt_title": "hi", "project_dir": "E:/automation/fleet-config",
            "alive": True, "started_at": "2026-07-27T07:14:00Z",
        }]
        healed = chief_router._reconcile_chief_labels(live, {})
        assert healed[0]["label"] == ""

    async def test_writes_the_durable_pointer_itself_issue_796(self, tmp_path):
        """Regression for #796: ``GET /api/board`` (``routers/board.py``)
        calls ``_reconcile_chief_labels`` directly, never
        ``_live_sessions_with_chief_label`` — so the pointer write must
        happen here too, or a chief self-healed only via this path (hand-
        typed into a plain Coding-tab session) never gets its conversation
        id persisted to ``webapp/chief-pointer.json``."""
        transcript = _transcript(tmp_path, "board-poll", typed=True)
        rows = {
            "board-poll-conv": _chief_state_row(
                "chief-old", _recent_iso(minutes=1),
                name="fleet-config-79", transcript_path=transcript,
            ),
        }
        import asyncio

        chief_router._reconcile_chief_labels(
            [_chief_row(
                label="", name="fleet-config", live_title="✳ chief",
                project_dir="E:/automation/fleet-config",
            )],
            rows,
        )
        pending = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        if pending:
            await asyncio.gather(*pending)
        assert chief_pointer.read_chief_pointer()["session_id"] == "board-poll-conv"


class TestChiefGate:

    def test_chief_routes_classified_passkey(self):
        from app.webapp.middleware import _terminal_guard_level
        assert _terminal_guard_level("/api/board/chief/ensure") == "passkey"
        assert _terminal_guard_level("/api/board/chief/settings") == "passkey"

    def test_ensure_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        assert client.post("/api/board/chief/ensure").status_code == 403
