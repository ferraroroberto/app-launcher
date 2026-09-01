"""Shared pytest fixtures for app-launcher.

Mirrors the pattern used by sister projects (voice-transcriber / photo-ocr):
build a fresh FastAPI ``create_app()`` against an isolated temp config dir,
with the expensive deps (session-host loopback client, audit log writer)
swapped for mocks. Tests run in-process via ``TestClient`` — no live tray,
no real session-host on :8446, no disk writes outside ``tmp_path``.

The live-tray Playwright suite lives separately under ``tests/e2e/`` and is
opt-in via ``pytest -m smoke``.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Redirect this repo's whole fleet runtime-data directory before any `src.*`
# module is imported, so `src.jobs_index.INDEX_DIR` — resolved at import time —
# never points at the real store during a test run.
#
# The per-test `_isolated_jobs_index` fixture below is the precise isolation;
# this is the backstop it needs. `jobs_index.ensure_index()` is dispatched via
# `asyncio.to_thread`, and a thread can land its write *after* monkeypatch has
# restored `INDEX_DIR`, so a purely function-scoped patch leaks the real
# production index intermittently — observed on roughly one full run in two.
# Setting the env var makes the module-level default itself safe, so a late
# thread has nowhere unsafe to write. `setdefault`, so a deliberate outer
# override still wins.
os.environ.setdefault(
    "APP_LAUNCHER_DATA_DIR",
    str(Path(tempfile.gettempdir()) / "app-launcher-test-runtime-data"),
)


# ------------------------------------------------- gate progress log (#534)
# When the pre-ship gate (scripts/verify-before-ship.ps1) sets
# LAUNCHER_VERIFY_PROGRESS_LOG, every test's start and finish is appended —
# and flushed — to that file as it happens. A gate that wedges or is killed
# by an outer timeout then leaves the ACTIVE node id (last START without a
# DONE), per-test totals (setup+call+teardown, so fixture cost is visible),
# and a slowest-tests summary on disk, instead of an opaque dead console.
# Inert for normal pytest runs (env var absent → every hook no-ops).

_PROGRESS_ENV = "LAUNCHER_VERIFY_PROGRESS_LOG"
_progress_t0 = time.monotonic()
_progress_node_totals: dict = {}
_progress_durations: list = []


def _progress_write(line: str) -> None:
    path = os.environ.get(_PROGRESS_ENV, "").strip()
    if not path:
        return
    stamp = time.strftime("%H:%M:%S")
    elapsed = time.monotonic() - _progress_t0
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp} +{elapsed:7.1f}s] {line}\n")
    except OSError:  # never let diagnostics fail the run
        pass


def pytest_runtest_logstart(nodeid, location) -> None:
    _progress_write(f"START {nodeid}")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    total = _progress_node_totals.get(report.nodeid, 0.0) + report.duration
    _progress_node_totals[report.nodeid] = total
    if report.when in ("setup", "call") and report.outcome != "passed":
        # skipped / failed / errored — name the phase so a fixture skip is
        # distinguishable from an assertion failure.
        _progress_write(f"{report.outcome.upper()} ({report.when}) {report.nodeid}")
    if report.when == "teardown":
        _progress_node_totals.pop(report.nodeid, None)
        _progress_durations.append((total, report.nodeid))
        _progress_write(f"DONE  {report.nodeid} ({total:.1f}s)")


def pytest_sessionfinish(session, exitstatus) -> None:
    if not os.environ.get(_PROGRESS_ENV, "").strip():
        return
    _progress_write(f"pytest session finished (exit status {exitstatus})")
    slowest = sorted(_progress_durations, key=lambda t: t[0], reverse=True)[:15]
    if slowest:
        _progress_write("slowest tests (setup+call+teardown):")
        for duration, nodeid in slowest:
            _progress_write(f"  {duration:7.1f}s  {nodeid}")


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(autouse=True)
def _no_real_mirror_window(request, monkeypatch):
    """Never spawn a real PC mirror window from a non-e2e test (issue #279).

    Both launch handlers — the Apps tab (``routers/apps.py``) and the Life OS
    tab (``routers/life_os.py``) — call ``open_local_terminal_window`` when
    ``should_mirror_to_pc`` is True, which it is under ``TestClient`` (its
    request host isn't loopback). Left real, each launch spawns an Edge
    ``--app`` window the unit test never tears down, littering the desktop with
    ``app-launcher-mirror-*`` windows. Stub the name as imported into each
    router so any launch test — present or future — is leak-proof from one
    place.

    The ``launcher`` module's own function is intentionally left intact so
    ``tests/test_launcher_mirror_hwnd.py`` can still exercise it directly with
    the inner spawn stubbed. Skipped for the live-tray e2e (``smoke``) suite,
    which drives a separate webapp process this in-process patch can't reach.
    """
    if request.node.get_closest_marker("smoke"):
        return
    for mod_name in (
        "app.webapp.routers.apps",
        "app.webapp.routers.life_os",
        "app.webapp.routers.board",
        "app.webapp.routers.board_chief",
    ):
        module = importlib.import_module(mod_name)
        if hasattr(module, "open_local_terminal_window"):
            monkeypatch.setattr(
                module, "open_local_terminal_window", lambda *a, **k: None
            )


@pytest.fixture(autouse=True)
def _isolated_jobs_index(tmp_path, monkeypatch):
    """Give each test its own Jobs index.

    The index used to live inside the run-history root, so patching
    ``jobs_history.JOBS_RUNS_DIR`` gave every test a private index for free —
    which ~20 tests and the opt-in ``webapp_client`` fixture relied on. Since
    project-scaffolding#243 the index no longer follows ``JOBS_RUNS_DIR``, so
    without this they would all share one index for the whole session and see
    each other's runs.

    Staying off the *real* index is the module-level ``APP_LAUNCHER_DATA_DIR``
    above, not this fixture — verified by disabling this fixture and confirming
    a jobs test still writes only into the temp root.
    """
    from src import jobs_index as jobs_index_mod

    monkeypatch.setattr(jobs_index_mod, "INDEX_DIR", tmp_path / "jobs_index")


@pytest.fixture(autouse=True)
def _isolated_chief_pointer(request, tmp_path, monkeypatch):
    """Never read or write the real chief pointer from a test (issue #675).

    ``src.chief_pointer`` resolves ``webapp/chief-pointer.json`` off the repo
    root, and the chief resume lookup consults it on every call — so without
    this, a pointer sitting on the dev box would silently steer
    ``_find_resumable_chief_session_id`` tests, and a test that exercised the
    write path would clobber the real standing chief's pointer. Autouse rather
    than opt-in for exactly that reason: the risk is to tests that never
    mention the pointer at all. Skipped for the ``smoke`` suite, which drives a
    separate webapp process this in-process patch can't reach.
    """
    if request.node.get_closest_marker("smoke"):
        return
    from app.webapp.routers import board_chief as chief_router
    from src import chief_pointer as chief_pointer_mod
    monkeypatch.setattr(
        chief_pointer_mod, "CHIEF_POINTER_FILE", tmp_path / "chief-pointer.json"
    )
    # The write-side memo is module-level and would otherwise leak one test's
    # chief into the next (a second test would then observe "no write").
    monkeypatch.setattr(chief_router, "_last_noted_chief_conversation", "")


@pytest.fixture
def sample_webapp_config() -> dict:
    """Parse the committed sample once per test."""
    return json.loads(
        (PROJECT_ROOT / "config" / "webapp_config.sample.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.fixture
def webapp_client(tmp_path: Path, monkeypatch) -> Iterator[tuple]:
    """Build a fresh launcher webapp wired to temp dirs + mocked deps.

    Yields ``(client, app, overrides)``:
      - ``client`` — fastapi.testclient.TestClient
      - ``app`` — the FastAPI instance (so tests can mutate ``app.state``)
      - ``overrides`` — dict of the mocks, so tests can configure return
        values / assert call args.

    Auth is disabled by default (``auth_token = ""``, ``auth_password = ""``).
    Auth tests opt back in by setting these on ``app.state.webapp_config``.
    """
    # The two on-disk configs the app reads on startup. Point both at temp
    # paths so a stray real config can never affect the test.
    tmp_apps_root = tmp_path / "scan_root"
    tmp_apps_root.mkdir()
    tmp_projects_dir = tmp_path / "projects_dir"
    tmp_projects_dir.mkdir()

    # webapp_config.json — empty file with valid defaults overlaid.
    tmp_webapp_cfg = tmp_path / "webapp_config.json"
    tmp_webapp_cfg.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 8445,
                "projects_dir": str(tmp_projects_dir),
                "apps_scan_root": str(tmp_apps_root),
                "claude_model": "opus",
                "claude_effort": "high",
                "claude_verbose": True,
                "claude_debug": False,
                "auth_token": "",
                "auth_password": "",
                "session_host_port": 8446,
                # Board tab (issue #300): point the sessions-state file at a
                # temp path so a test can never read the real hook-written
                # ~/.claude/hooks/state/sessions-state.json.
                "sessions_state_file": str(tmp_path / "sessions-state.json"),
                # Same reasoning for the rate-limits cache (issue #326): never
                # let a test touch ~/.claude/hooks/state/rate-limits.json.
                "rate_limits_file": str(tmp_path / "rate-limits.json"),
                # Context filter (issue #713): never let a test touch the
                # real machine-wide ~/.fleet-context-filter/{mode.json,
                # shadow.jsonl} — same isolation reasoning as the two files
                # above.
                "context_filter_mode_file": str(tmp_path / "context-filter-mode.json"),
                "context_filter_log_file": str(tmp_path / "context-filter-shadow.jsonl"),
                "github_owner": "testowner",
            }
        ),
        encoding="utf-8",
    )
    from src import webapp_config as webapp_cfg_mod
    monkeypatch.setattr(webapp_cfg_mod, "DEFAULT_CONFIG_PATH", tmp_webapp_cfg)

    # apps.json — start empty so registry tests own their fixtures.
    tmp_registry = tmp_path / "apps.json"
    from src import registry as registry_mod
    monkeypatch.setattr(registry_mod, "DEFAULT_REGISTRY_PATH", tmp_registry)

    # jobs.json + webapp/jobs/ — same isolation as apps.json. Both
    # belong to the Jobs tab (issue #47); the API router reads
    # DEFAULT_JOBS_PATH and writes run records under JOBS_RUNS_DIR.
    tmp_jobs_cfg = tmp_path / "jobs.json"
    tmp_jobs_runs = tmp_path / "jobs_runs"
    from src import jobs_config as jobs_cfg_mod
    from src import jobs_history as jobs_history_mod
    monkeypatch.setattr(jobs_cfg_mod, "DEFAULT_JOBS_PATH", tmp_jobs_cfg)
    # JOBS_RUNS_DIR is owned by src.jobs_history (issue #315 split) — patch
    # it there, not on the src.jobs facade, so runs_dir()/list_runs()/etc.
    # (which read their own module's global, not the facade's copy) see it.
    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_jobs_runs)

    # app_config.json — also redirect to tmp so create_app's load_app_config
    # doesn't read the real one. The launcher's app_config has very little
    # surface, so an empty file is fine; load_app_config defaults the rest.
    tmp_app_cfg = tmp_path / "config.json"
    tmp_app_cfg.write_text("{}", encoding="utf-8")
    from src import app_config as app_cfg_mod
    if hasattr(app_cfg_mod, "DEFAULT_CONFIG_PATH"):
        monkeypatch.setattr(app_cfg_mod, "DEFAULT_CONFIG_PATH", tmp_app_cfg)

    # Now import the server + routers. Important: import after monkeypatching
    # the config paths, but before patching session_client / audit (which are
    # module-level references inside each router that talks to them).
    from app.webapp import server as server_mod
    from app.webapp.routers import apps as apps_router
    from app.webapp.routers import board as board_router
    from app.webapp.routers import board_chief as board_chief_router
    from app.webapp.routers import board_spawn as board_spawn_router
    from app.webapp.routers import life_os as life_os_router
    from app.webapp.routers import voice_ocr_tts as voice_ocr_tts_router
    from app.webapp.routers import misc as misc_router
    from app.webapp.routers import sessions as sessions_router

    # Mock the session-host loopback client. Every route that talks to
    # :8446 goes through this module, so patch each router that holds a
    # module-level `session_client` reference of its own. The launch
    # routers (apps.py, life_os.py) no longer do since #689 moved their
    # spawn + error-mapping head into _helpers.spawn_session_or_400 —
    # they reach the host only through the `spawn_claude_session` the
    # per-test fakes replace.
    from src import session_client as real_session_client
    session_mock = MagicMock()
    session_mock.list_sessions.return_value = []
    session_mock.stop.return_value = {"ok": True}
    session_mock.create_session.return_value = {
        "session_id": "test-session-1",
        "kind": "pty",
    }
    session_mock.upload_image.return_value = {"path": "stub.png"}
    # Default: no session-host in the test env (#615) — /api/version's
    # freshness check degrades to "unreachable", never a real loopback call.
    session_mock.identity.return_value = None
    session_mock.SessionHostError = real_session_client.SessionHostError
    monkeypatch.setattr(sessions_router, "session_client", session_mock)
    monkeypatch.setattr(board_router, "session_client", session_mock)
    # The Board's chief lifecycle and its shared spawn-then-type mechanics
    # each hold their own module-level reference since the #691 split.
    monkeypatch.setattr(board_chief_router, "session_client", session_mock)
    monkeypatch.setattr(board_spawn_router, "session_client", session_mock)
    monkeypatch.setattr(misc_router, "session_client", session_mock)

    # Mock the voice-transcriber loopback client (issue #165) — the
    # /api/transcribe proxy goes through it; tests assert call args and set
    # the transcript without a live voice-transcriber on :8443. Lives in
    # routers/voice_ocr_tts.py (split off routers/sessions.py, #521).
    from src import voice_client as real_voice_client
    voice_mock = MagicMock()
    voice_mock.transcribe.return_value = {"transcript": "stub text", "language": "en"}
    voice_mock.create_session.return_value = {"session_id": "vt-stub"}
    voice_mock.send_chunk.return_value = {"raw_bytes": 0}
    voice_mock.finish.return_value = {"transcript": "stub text", "language": "en"}
    voice_mock.events_url.side_effect = (
        lambda base, sid: f"{base.rstrip('/')}/api/sessions/{sid}/events"
    )
    voice_mock.VoiceTranscriberError = real_voice_client.VoiceTranscriberError
    monkeypatch.setattr(voice_ocr_tts_router, "voice_client", voice_mock)

    # Mock the photo-ocr loopback client (issue #171) — the /api/ocr proxy
    # goes through it; tests assert call args and set the extracted text
    # without a live photo-ocr on :8444. Lives in routers/voice_ocr_tts.py.
    from src import photo_ocr_client as real_photo_ocr_client
    photo_ocr_mock = MagicMock()
    photo_ocr_mock.extract.return_value = {
        "text": "stub ocr text", "model": "gemini_flash", "session_id": "po-stub"
    }
    photo_ocr_mock.PhotoOcrError = real_photo_ocr_client.PhotoOcrError
    monkeypatch.setattr(voice_ocr_tts_router, "photo_ocr_client", photo_ocr_mock)

    # Mock the local-llm-hub TTS loopback client (issue #203) — the
    # /api/tts/health probe goes through it; /api/tts/speak streams via httpx
    # (mocked per-test). Tests assert health/payload without a live hub on
    # :8000. build_speech_payload / speech_url keep their real behaviour so
    # the proxy builds the correct upstream call. Lives in
    # routers/voice_ocr_tts.py.
    from src import tts_client as real_tts_client
    tts_mock = MagicMock()
    tts_mock.health.return_value = True
    tts_mock.speech_url.side_effect = real_tts_client.speech_url
    tts_mock.build_speech_payload.side_effect = real_tts_client.build_speech_payload
    tts_mock.TtsError = real_tts_client.TtsError
    monkeypatch.setattr(voice_ocr_tts_router, "tts_client", tts_mock)

    # Mock the local-llm-hub chat client (issue #210) — the /api/tts/summarize
    # proxy goes through it; tests assert the summary without a live hub on
    # :8000. summarize() returns a stub by default; LlmError keeps its real
    # type so error-mapping tests can raise it. Lives in
    # routers/voice_ocr_tts.py.
    from src import llm_client as real_llm_client
    llm_mock = MagicMock()
    llm_mock.summarize.return_value = "Build is green. No decision needed."
    llm_mock.LlmError = real_llm_client.LlmError
    monkeypatch.setattr(voice_ocr_tts_router, "llm_client", llm_mock)

    # Audit log writer — stub so no files land in webapp/sessions/ during
    # tests. The real audit module opens log files lazily. The `audit`
    # import lives in routers/apps.py, routers/sessions.py,
    # routers/voice_ocr_tts.py (split off sessions.py, #521), and
    # routers/webauthn.py — patch all four.
    audit_mock = MagicMock()
    from app.webapp.routers import webauthn as webauthn_router
    monkeypatch.setattr(apps_router, "audit", audit_mock)
    monkeypatch.setattr(sessions_router, "audit", audit_mock)
    monkeypatch.setattr(voice_ocr_tts_router, "audit", audit_mock)
    monkeypatch.setattr(webauthn_router, "audit", audit_mock)
    monkeypatch.setattr(life_os_router, "audit", audit_mock)
    monkeypatch.setattr(board_router, "audit", audit_mock)
    monkeypatch.setattr(board_chief_router, "audit", audit_mock)

    # WebAuthnGate doesn't touch disk until configured (rp_id + origin set)
    # so default tests are safe. We still stub it for the few endpoints that
    # poke at .configured() to keep behaviour deterministic.
    webauthn_mock = MagicMock()
    webauthn_mock.configured.return_value = False
    monkeypatch.setattr(server_mod, "WebAuthnGate", lambda: webauthn_mock)

    # Build the app fresh. ``create_app()`` calls load_webapp_config /
    # load_app_config / load_registry — all redirected above.
    app = server_mod.create_app()

    # Auth off by default. Tests that want it on do:
    #     app.state.webapp_config.auth_token = "secret"
    #     app.state.webapp_config.auth_password = "hunter2"
    app.state.webapp_config.auth_token = ""
    app.state.webapp_config.auth_password = ""

    from fastapi.testclient import TestClient
    client = TestClient(app)

    overrides = {
        "session": session_mock,
        "voice": voice_mock,
        "photo_ocr": photo_ocr_mock,
        "tts": tts_mock,
        "llm": llm_mock,
        "audit": audit_mock,
        "webauthn": webauthn_mock,
        "tmp_registry_path": tmp_registry,
        "tmp_apps_scan_root": tmp_apps_root,
        "tmp_projects_dir": tmp_projects_dir,
        "tmp_webapp_cfg_path": tmp_webapp_cfg,
        "tmp_jobs_path": tmp_jobs_cfg,
        "tmp_jobs_runs_dir": tmp_jobs_runs,
    }
    yield client, app, overrides
