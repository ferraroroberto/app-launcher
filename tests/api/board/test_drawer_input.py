"""POST /api/claude-code/sessions/{sid}/input: the reply proxy, its passkey gate and a stale session-host."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import subprocess_flags


# ----------------------------------------------------------- passkey gates


def test_new_paths_classified_passkey():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/input") == "passkey"
    assert _terminal_guard_level("/api/board/sessions/abc/exchange") == "passkey"
    assert _terminal_guard_level("/api/board/issues/start") == "passkey"


class TestGateRefusal:
    """The TestClient connects as host 'testclient' (not loopback, not
    tailnet) — all three #301 endpoints must be refused outright."""

    def test_input_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input", json={"data": "hi"}
        )
        assert resp.status_code == 403

    def test_exchange_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/board/sessions/s1/exchange").status_code == 403

    def test_issue_start_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "x", "number": 1, "mode": "start"},
        )
        assert resp.status_code == 403


# ------------------------------------------------------------- reply proxy


class TestInputProxy:

    def test_multiline_forwarded_raw_in_one_call(self, webapp_client, _bypass_gate):
        """Framing + the submit CR are the session-host's own job now
        (#611) — the router just forwards data + submit in a single call."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "line one\nline two", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "line one\nline two", True)

    def test_single_line_forwarded_raw(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "hello", "submit": True},
        )
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "hello", True)

    def test_no_submit_forwards_submit_false(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "draft", "submit": False},
        )
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "draft", False)

    def test_blank_data_without_submit_is_400(self, webapp_client, _bypass_gate):
        """Blank data with no submit is a genuine no-op request — nothing
        to write, nothing to submit — unlike the bare-submit escape hatch
        below, which always carries submit=True."""
        client, _, _ = webapp_client
        assert client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "   ", "submit": False},
        ).status_code == 400
        assert client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "", "submit": False},
        ).status_code == 400

    def test_bare_submit_escape_hatch_releases_stranded_composer(
        self, webapp_client, _bypass_gate
    ):
        """{"data": "", "submit": true} (#611) — release whatever is already
        sitting in the composer, with no text write. The recovery path for a
        message stranded by the submit race, previously only reachable by
        tapping the phone's own compose Send by hand."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "", True)

    def test_whitespace_only_data_with_submit_is_bare_submit(
        self, webapp_client, _bypass_gate
    ):
        """Whitespace-only data collapses to the same bare-submit call as
        an empty string — there is no meaningful text to write either way."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "   ", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert calls[0].args == (8446, "s1", "", True)

    def test_dead_session_surfaces_as_error_not_false_ok(
        self, webapp_client, _bypass_gate
    ):
        """A session-host 409 (write dropped, issue #607) must propagate as a
        real error — never collapse back to {"ok": true}."""
        client, _, overrides = webapp_client
        overrides["session"].send_input.side_effect = (
            overrides["session"].SessionHostError(
                "session s1 not accepting input (exited)", status=409
            )
        )

        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "hello", "submit": True},
        )

        assert resp.status_code == 409
        assert resp.json() != {"ok": True}


class TestSessionInputStaleHost:
    """#967 reopen: a detached Send against a session-host that predates
    detached-session input came back as a bare "session-host HTTP 500".
    The route now names that condition — and only that condition — from
    facts it can establish (the target's kind, the host's loaded sha),
    keeping "stale host", "new path failed" and "couldn't tell" distinct."""

    _FEATURE = "1cf17209c86885ff3a488bd2501e26e8a69c4580"

    def _post_after_500(self, webapp_client, monkeypatch, *, kind, contains):
        from app.webapp.routers import sessions as sessions_router

        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.send_input.side_effect = sess.SessionHostError(
            "session-host HTTP 500", status=500
        )
        sess.get_session.return_value = {"session_id": "sid-967", "kind": kind}
        sess.identity.return_value = {
            "git_sha": "5ea6ef8", "started_at": "2026-09-13T09:36:27",
        }
        calls = []

        def _contains(repo, sha, commit):
            calls.append((sha, commit))
            return contains

        monkeypatch.setattr(
            sessions_router, "sha_contains_commit", _contains, raising=False
        )
        resp = client.post(
            "/api/claude-code/sessions/sid-967/input",
            json={"data": "what is this project?", "submit": True},
        )
        return resp, calls

    def test_detached_send_to_a_predating_host_names_the_restart(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        resp, calls = self._post_after_500(
            webapp_client, monkeypatch, kind="remote", contains=False
        )
        assert resp.status_code == 501
        detail = resp.json()["detail"]
        assert "session-host restart needed" in detail
        assert "5ea6ef8" in detail
        assert "HTTP 500" not in detail
        assert calls == [("5ea6ef8", self._FEATURE)]

    def test_detached_send_failing_on_a_current_host_passes_the_real_error(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        # The new path genuinely failed — never mislabel that as "restart".
        resp, _ = self._post_after_500(
            webapp_client, monkeypatch, kind="remote", contains=True
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "session-host HTTP 500"

    def test_unknown_host_lineage_is_reported_as_unknown_not_stale(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        resp, _ = self._post_after_500(
            webapp_client, monkeypatch, kind="remote", contains=None
        )
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert detail.startswith("session-host HTTP 500")
        assert "could not confirm" in detail
        assert "restart needed" not in detail

    def test_pty_target_is_never_classified(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        # Every session-host has had PTY input — a PTY 500 is not staleness.
        resp, calls = self._post_after_500(
            webapp_client, monkeypatch, kind="pty", contains=False
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "session-host HTTP 500"
        assert calls == []

    def test_pinned_commit_is_the_one_that_added_detached_input(self):
        # A typo'd pin would silently degrade every detached 500 to
        # "could not confirm". CI's depth-1 checkout lacks the object.
        import subprocess

        from app.webapp.routers import sessions as sessions_router

        repo = Path(__file__).resolve().parents[3]
        pin = sessions_router._DETACHED_INPUT_COMMIT
        assert pin == self._FEATURE

        def show(rev: str) -> "str | None":
            out = subprocess.run(
                ["git", "-C", str(repo), "show", f"{rev}:src/session_host.py"],
                capture_output=True, text=True, encoding="utf-8", creationflags=subprocess_flags.NO_WINDOW,
            )
            return out.stdout if out.returncode == 0 else None

        at_pin = show(pin)
        if at_pin is None:
            pytest.skip("pinned commit not in this (shallow) clone")
        before = show(f"{pin}~1")
        assert "class RemoteInputOutcome" in at_pin
        assert before is not None and "class RemoteInputOutcome" not in before

    def test_non_500_errors_skip_the_lineage_check(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        from app.webapp.routers import sessions as sessions_router

        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.send_input.side_effect = sess.SessionHostError(
            "session sid-967 not accepting input (exited)", status=409
        )
        monkeypatch.setattr(
            sessions_router, "sha_contains_commit",
            lambda *a: pytest.fail("lineage must not be checked for a 409"),
            raising=False,
        )
        resp = client.post(
            "/api/claude-code/sessions/sid-967/input",
            json={"data": "hi", "submit": True},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "session sid-967 not accepting input (exited)"
        sess.get_session.assert_not_called()
