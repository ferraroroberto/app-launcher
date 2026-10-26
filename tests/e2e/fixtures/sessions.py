"""Opt-in fixtures that launch a PTY session, and the pollers that read its log."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Iterator

import pytest
import requests
from playwright.sync_api import Page

from src.scanner import slugify

from tests.e2e.fixtures.env import AUTOBOOT_STATE, REPO_ROOT, SESSIONS_DIR, autoboot_enabled
from tests.e2e.fixtures.launch_target import require_trusted_launch_target
from tests.e2e.stub_session import STUB_FLAG as _STUB_FLAG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- session API
# Opt-in fixtures: tests that need state in #sessionsList depend on one of
# these; other tests don't pay any launch + teardown cost. The lightweight stub
# session runs in THIS checkout; the real-agent session runs in the first
# agent-trusted checkout of this repository (see `launch_target_dir`).
# Self-launching is harmless — it just spawns the agent in a repo dir, and the
# real-agent pin never submits a prompt.
#
# Neither is the literal `app-launcher` any more (issue #932). A
# merge-verification run works from a fresh detached checkout under a scratch
# root (`E:\tmp\al-merge-<date>`), where no sibling directory is called
# `app-launcher` at all — so the hardcoded id resolved to no coding row, the
# launch 404'd, and the real-agent tests *skipped* while the gate printed the
# same green as a run that covered them.
_CHECKOUT_ID = slugify(REPO_ROOT.name)


def auth_headers(auth_token: str) -> dict:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def _stop_session(base_url: str, headers: dict, sid: str) -> None:
    """Force-kill a PTY session. `mode: "kill"` is unconditional (vs "quit",
    which waits for claude to process /quit). Best-effort — a swallowed
    exception here must not mask the actual test failure."""
    try:
        requests.post(
            f"{base_url}/api/claude-code/sessions/{sid}/stop",
            json={"mode": "kill"},
            headers=headers,
            verify=False,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  session %s teardown failed: %s", sid, exc)


def _launch_claude_via_webapp(
    base_url: str, auth_token: str, *, autoboot: bool = False
) -> str:
    """Launch a REAL claude PTY session through the webapp's launch endpoint.

    Where `claude` isn't on PATH — notably the CI runner, which never
    installs it — the PTY child exits at once ("'claude' is not
    recognized…"), the session-host reaps it, and its WS endpoint then 403s
    the webapp's proxy, so every consumer would race a corpse. Skip cleanly
    instead: these tests genuinely gate on a dev box where `claude` runs.
    The test process shares the live session-host's PATH (same machine), so
    `which` here faithfully predicts whether the session-host can spawn it.
    See #58.

    The target is the first agent-trusted checkout of this repository — a
    skip naming trust when there is none (issue #932).
    """
    if shutil.which("claude") is None:
        pytest.skip(
            "`claude` is not on PATH — real-Claude PTY tests need a live "
            "claude CLI and skip cleanly where it isn't installed (e.g. the "
            "CI runner)"
        )
    target_id = slugify(require_trusted_launch_target().name)

    headers = auth_headers(auth_token)
    try:
        res = requests.post(
            f"{base_url}/api/apps/{target_id}/launch",
            # The test renders the session in its own Playwright page, so say
            # so: without `in_page` the launch mirrors to a real PC Edge window
            # (issue #938; the disposable instance also refuses to mirror).
            json={"mode": "pty", "in_page": True},
            headers=headers,
            verify=False,
            timeout=10,
        )
    except Exception as exc:
        pytest.skip(f"launch request failed: {exc.__class__.__name__}: {exc}")

    if res.status_code != 200:
        detail = f"HTTP {res.status_code}: {res.text[:200]}"
        if autoboot:
            # Under the gate the disposable config pins the scan root to the
            # launch target's parent, so the row always exists — a non-200
            # here is a harness bug, never a missing dependency. It used to
            # skip, which is how #932's fresh-checkout 404 ("unknown app
            # app-launcher") cost two tests while the gate still printed green.
            pytest.fail(
                f"could not launch PTY session for target {target_id!r} "
                f"({detail}) — under autoboot this is a harness bug (issue #932)"
            )
        # 400 is the expected failure when the project_dir is invalid — skip
        # cleanly rather than fail the suite.
        pytest.skip(f"could not launch PTY session ({detail})")

    body = res.json()
    sid = body.get("session", {}).get("session_id")
    if not sid:
        pytest.skip(f"launch response missing session_id: {body}")
    return str(sid)


@pytest.fixture
def launched_pty_session(
    request: pytest.FixtureRequest, base_url: str, auth_token: str
) -> Iterator[str]:
    """A live PTY session for UI-only assertions (issue #534).

    Under autoboot (the pre-ship gate + CI) the child is the deterministic
    lightweight stub, created directly on the disposable session-host with
    the ``--e2e-stub`` sentinel — no real Claude CLI process per test. The
    launch API never blocked on Claude's bootstrap, so the win is not big
    idle-box wall time (measured ~20 s across the whole gate, #534): it is
    removing ~110 background node boots whose CPU contention made loaded
    runs balloon, plus CI coverage (the stub needs only Python). The
    production webapp ↔ session-host ↔ ConPTY boundary stays fully real
    (session rows, WS streaming, input forwarding, stop paths).

    Against the LIVE tray (run-e2e.ps1 dev loop) there is no shim on the
    tray's PATH, so this falls back to a real claude launch — behaviour
    identical to before the split.

    Tests that assert real agent semantics (rendered Claude output, agent
    echo, lifecycle) must use `launched_claude_pty_session` instead.
    """
    headers = auth_headers(auth_token)
    if autoboot_enabled(request.config):
        sh_port = AUTOBOOT_STATE.get("session_host_port")
        if not sh_port:
            pytest.fail("autoboot state missing session_host_port (issue #534)")
        # POST the session-host directly: the sentinel flag can't travel
        # through the webapp's launch endpoint (flags come from config
        # there). The session still surfaces through the webapp normally —
        # its session list proxies this same host.
        res = requests.post(
            f"http://127.0.0.1:{sh_port}/sessions",
            json={
                "project_dir": str(REPO_ROOT),
                "name": _CHECKOUT_ID,
                "flags": _STUB_FLAG,
                "agent": "claude",
            },
            # The listener is already health-checked; the response waits for
            # ConPTY creation, measured at 19 s on this host under gate load.
            timeout=(5, 30),
        )
        # Deterministic path — a failure here is a harness bug, never a
        # missing-dependency skip.
        if res.status_code != 200:
            pytest.fail(
                f"lightweight stub session failed to launch (HTTP "
                f"{res.status_code}: {res.text[:200]})"
            )
        sid = str(res.json().get("session_id") or "")
        if not sid:
            pytest.fail(f"stub session response missing session_id: {res.text[:200]}")
    else:
        sid = _launch_claude_via_webapp(base_url, auth_token)

    try:
        yield sid
    finally:
        _stop_session(base_url, headers, sid)


@pytest.fixture
def launched_claude_pty_session(
    request: pytest.FixtureRequest, base_url: str, auth_token: str
) -> Iterator[str]:
    """A live PTY session running the REAL Claude CLI (issue #534).

    Only for tests whose assertions depend on the real agent — rendered
    Claude output in the xterm buffer, agent input echo, Claude lifecycle
    semantics. Spawns a real node process per test: keep its consumer set
    minimal, and put UI-only assertions on `launched_pty_session`.

    Precondition: some checkout of this repository must already have cleared
    the agent's folder-trust gate (issue #932) — a fresh detached clone has
    not, and the agent paints its trust prompt where the composer should be.
    """
    headers = auth_headers(auth_token)
    sid = _launch_claude_via_webapp(
        base_url, auth_token, autoboot=autoboot_enabled(request.config)
    )
    try:
        yield sid
    finally:
        _stop_session(base_url, headers, sid)


# ----------------------------------------------------- input-delivery polling
# Env-aware so the slow hosted CI runner gets headroom without slowing local
# runs (issue #184, finishing #58): the ConPTY round-trip (keystroke → session
# host → log flush) lands well within 5 s locally but can exceed it on a loaded
# windows-2025 runner. e2e.yml sets E2E_LOG_POLL_DEADLINE_MS larger for CI.
_LOG_POLL_DEADLINE_MS = int(os.environ.get("E2E_LOG_POLL_DEADLINE_MS", "5000"))


@pytest.fixture
def wait_for_session_log() -> Callable[..., bool]:
    """Return a poller for the per-session input log.

    ``wait(page, sid, needle, deadline_ms=_LOG_POLL_DEADLINE_MS)`` reads
    ``<sessions dir>/<sid>.log`` every 200 ms until ``needle`` appears or the
    deadline elapses, then returns ``True``/``False``. The directory is the
    autoboot temp dir when the gate redirected it (issue #913), else the
    checkout's own ``webapp/sessions`` — live mode reads the real one. One
    source of truth for the input-delivery wait that used to be a hardcoded
    5 s poll loop copied into four test files (issue #58).
    """

    def _wait(
        page: Page,
        sid: str,
        needle: str,
        deadline_ms: int = _LOG_POLL_DEADLINE_MS,
    ) -> bool:
        sessions_dir = AUTOBOOT_STATE.get("sessions_dir", SESSIONS_DIR)
        log_path = sessions_dir / f"{sid}.log"

        def _hit() -> bool:
            return log_path.exists() and needle in log_path.read_text(
                encoding="utf-8", errors="replace"
            )

        for _ in range(max(1, deadline_ms // 200)):
            if _hit():
                return True
            page.wait_for_timeout(200)
        # Final read so a hit landing in the last 200 ms interval isn't missed.
        return _hit()

    return _wait


@pytest.fixture
def session_log_path() -> Callable[[str], Path]:
    """Return ``sid -> <sessions dir>/<sid>.log``.

    Same directory resolution as ``wait_for_session_log`` — autoboot temp dir
    when the gate redirected it (issue #913), else the checkout's own
    ``webapp/sessions`` — exposed for the assertions that must *count* logged
    chunks rather than wait for one to appear (issue #1024: one Ctrl+C tap
    delivers exactly one ``\\x03``, never two).
    """

    def _path(sid: str) -> Path:
        sessions_dir = AUTOBOOT_STATE.get("sessions_dir", SESSIONS_DIR)
        return sessions_dir / f"{sid}.log"

    return _path
