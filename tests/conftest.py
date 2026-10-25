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
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Redirect this repo's whole fleet runtime-data directory before any `src.*`
# module is imported, so `src.jobs.index.INDEX_DIR` — resolved at import time —
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

# Same shape, same reason, for the audit module's session files (issue #913).
# `src.audit` resolves its directory from `__file__`, so the real-PTY tests
# that drive a genuine ConPTY through `PtySession` — the submit probes and the
# #64 readback — wrote their `<id>.transcript` into the checkout's own
# `webapp/sessions`, the directory the phone reads. Small next to what the e2e
# gate used to add there (5 files per run against a measured 228), but the
# same defect, and it is the half a browser-suite fix can't reach: these
# sessions live inside the pytest process, so the isolation has to be in place
# before `src.audit` is imported. `setdefault`, so a deliberate outer override
# still wins — and so the e2e autoboot's own per-run override still applies to
# the processes it spawns.
os.environ.setdefault(
    "LAUNCHER_AUDIT_DIR",
    str(Path(tempfile.gettempdir()) / "app-launcher-test-sessions"),
)

# Credential hygiene (issue #907): every test report — browser suite included —
# is scrubbed of credentials before any reporter writes it. Re-exported so
# pytest registers the hook from this conftest; imported only after the
# runtime-data redirect above, since it pulls in `src.webapp_config`.
from tests._credential_hygiene import pytest_runtest_makereport  # noqa: E402,F401

# Gate progress log (issues #534, #943): per-test START/DONE lines, a bounded
# traceback excerpt under each FAILED line, and a slowest-tests summary, written
# to LAUNCHER_VERIFY_PROGRESS_LOG when the pre-ship gate sets it. Re-exported so
# pytest registers the hooks from this conftest; the summary is written from
# `pytest_sessionfinish` below.
from tests import _progress_log  # noqa: E402
from tests._progress_log import (  # noqa: E402,F401
    pytest_runtest_logreport,
    pytest_runtest_logstart,
)


# --- tests/ source-mutation guard (#822) -------------------------------------
#
# A test that silently rewrites a tracked source file and still reports green is
# worse than a failing test. `_pty_readback_child.py` did exactly that: a spawn
# argument was mangled, the child truncated its own source, 3 tests quietly
# stopped existing, and the run was green. Nothing noticed until an unrelated
# `git diff` showed `43 ---------`.
#
# So: fingerprint every `.py` under tests/ at session start and re-check at
# finish. This is a whole-directory invariant rather than a fix for that one
# helper, because the class of bug — a test process writing to a path it
# resolved wrongly — is not specific to it.
_TESTS_DIR = Path(__file__).resolve().parent
_SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules"}
_source_fingerprints: dict[Path, int] = {}


def _iter_test_sources() -> Iterator[Path]:
    for path in _TESTS_DIR.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(_TESTS_DIR).parts):
            continue
        yield path


def _fingerprint_test_sources() -> dict[Path, int]:
    """Map each test source to a cheap content fingerprint (CRC32 of its bytes).

    CRC32 rather than a cryptographic hash: this is corruption detection, not
    tamper detection, and it keeps the whole sweep well under a tenth of a
    second for ~200 files.
    """
    import zlib

    prints: dict[Path, int] = {}
    for path in _iter_test_sources():
        try:
            prints[path] = zlib.crc32(path.read_bytes())
        except OSError:
            continue
    return prints


def pytest_sessionstart(session) -> None:
    _source_fingerprints.update(_fingerprint_test_sources())


def _check_test_sources_unmodified(session) -> None:
    """Fail the run if it mutated or deleted a test source it did not create."""
    if not _source_fingerprints:
        return
    after = _fingerprint_test_sources()
    changed = sorted(
        str(p.relative_to(_TESTS_DIR))
        for p, before in _source_fingerprints.items()
        if after.get(p) != before
    )
    strays = sorted(
        str(p.relative_to(_TESTS_DIR))
        for p in _TESTS_DIR.rglob("*.ready")
        if not any(part in _SKIP_DIRS for part in p.relative_to(_TESTS_DIR).parts)
    )
    if not changed and not strays:
        return
    lines = ["the tests/ tree changed while this run was in flight:"]
    for name in changed:
        lines.append(f"  modified/deleted: {name}")
    for name in strays:
        lines.append(f"  stray artifact:   {name}")
    # Two causes produce this, and the guard cannot tell them apart: a test that
    # wrote to a path it resolved wrongly (the #822 bug), or someone editing a
    # test file while the suite ran. Say both rather than accuse the suite —
    # a guard that misattributes its own trigger trains people to ignore it.
    lines.append("")
    lines.append("Either a test wrote to a path it resolved wrongly, or a file was")
    lines.append("edited mid-run. If you were not editing, treat it as the former:")
    lines.append("  git status tests/   then   git checkout -- tests/")
    message = "\n".join(lines)
    # Print as well as fail: a non-zero exit status alone is easy to misread as
    # an ordinary test failure, and the actionable part is *which* file moved.
    print(f"\n❌ {message}", file=sys.stderr)
    session.exitstatus = 1


def pytest_sessionfinish(session, exitstatus) -> None:
    _check_test_sources_unmodified(session)
    _progress_log.write_session_summary(exitstatus)


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(autouse=True)
def _no_real_mirror_window(request, monkeypatch):
    """Never spawn a real PC mirror window from a non-e2e test (issue #279).

    Both launch handlers — the Apps tab (``routers/apps.py``) and the Life OS
    tab (``routers/life_os_spawn.py``'s shared spawn tail since the #884
    split) — call ``open_local_terminal_window`` when
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
        "app.webapp.routers.life_os_spawn",
        "app.webapp.routers.board",
        "app.webapp.routers.board_chief",
    ):
        module = importlib.import_module(mod_name)
        if hasattr(module, "open_local_terminal_window"):
            monkeypatch.setattr(
                module, "open_local_terminal_window", lambda *a, **k: None
            )


@pytest.fixture(autouse=True)
def fake_injector(monkeypatch):
    """Never inject real mouse or keyboard input from a test (#1521).

    Every Screen-control event goes out through ``desktop_input.get_injector``;
    here it is a recorder for the whole suite, so no test can move the
    owner's mouse or type into their desktop. A test that asserts on what
    would have been sent asks for this fixture by name. The e2e suite's
    disposable webapp gets the same through ``LAUNCHER_SCREEN_FAKE_INPUT=1``.
    """
    from src.desktop import input as desktop_input

    recorder = desktop_input.RecordingInjector()
    monkeypatch.setattr(desktop_input, "get_injector", lambda: recorder)
    monkeypatch.setattr(desktop_input, "_injector", recorder)
    return recorder


@pytest.fixture(autouse=True)
def fake_clipboard(monkeypatch):
    """Never touch the real Windows clipboard from a test (#1719).

    The Screen view's ``clipboard_set`` goes out through
    ``desktop_clipboard.get_clipboard``; here it is a recorder for the whole
    suite, so no test can overwrite what the owner just copied. A test that
    asserts on what would have been copied asks for this fixture by name. The
    e2e suite's disposable webapp gets the same through
    ``LAUNCHER_SCREEN_FAKE_INPUT=1``.
    """
    from src.desktop import clipboard as desktop_clipboard

    recorder = desktop_clipboard.RecordingClipboard()
    monkeypatch.setattr(desktop_clipboard, "get_clipboard", lambda: recorder)
    monkeypatch.setattr(desktop_clipboard, "_clipboard", recorder)
    return recorder


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    """Run history, the run queue and ``jobs.json`` all under ``tmp_path``.

    ``JOBS_RUNS_DIR`` / ``JOBS_QUEUE_PATH`` are owned by ``src.jobs.history`` /
    ``src.jobs.queue``, so they are patched there, not on the ``src.jobs`` facade.
    """
    from src.jobs import config as jc
    from src.jobs import history as jobs_history_mod
    from src.jobs import queue as jobs_queue_mod

    monkeypatch.setattr(jobs_history_mod, "JOBS_RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_queue_mod, "JOBS_QUEUE_PATH", tmp_path / "_queue.json")
    monkeypatch.setattr(jc, "DEFAULT_JOBS_PATH", tmp_path / "jobs.json")
    return tmp_path


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
    from src.jobs import index as jobs_index_mod

    monkeypatch.setattr(jobs_index_mod, "INDEX_DIR", tmp_path / "jobs_index")


@pytest.fixture(autouse=True)
def _fresh_jobs_snapshot(monkeypatch):
    """Every test starts with an empty jobs runtime snapshot (#1324), and by
    default every read rebuilds it, so a test that writes a run and then GETs
    ``/api/jobs`` sees it as it did before the snapshot existed. The
    background tick never builds under test: a build left running on one
    test's temp dirs must not land in the next test's snapshot. The
    snapshot's own tests (``test_jobs_snapshot.py``) put the real ages back.
    """
    from src.jobs import snapshot as jobs_snapshot

    jobs_snapshot.reset()
    monkeypatch.setattr(jobs_snapshot, "MAX_AGE_SECONDS", -1.0)
    monkeypatch.setattr(jobs_snapshot, "DEMAND_WINDOW_SECONDS", -1.0)
    yield
    jobs_snapshot.reset()


@pytest.fixture(autouse=True)
def _isolated_jobs_registry(request, tmp_path, monkeypatch):
    """Never let a test's run-record read consult the real ``jobs.json``.

    Since #1316 every ``jobs_history.read_run`` looks up the owning job's
    ``exit_outcomes`` in the registry, so a test that only redirects
    ``JOBS_RUNS_DIR`` would otherwise classify its runs against whatever the
    dev box's ``config/jobs.json`` declares. Tests that write a registry of
    their own re-patch ``DEFAULT_JOBS_PATH`` on top of this. Skipped for the
    ``smoke`` suite, which drives a separate webapp process.
    """
    if request.node.get_closest_marker("smoke"):
        return
    from src.jobs import config as jobs_cfg_mod
    from src.jobs import dismissed as jobs_dismissed_mod

    monkeypatch.setattr(
        jobs_cfg_mod, "DEFAULT_JOBS_PATH", tmp_path / "isolated-jobs.json"
    )
    # Nor the dismissed-alert marks (#1669): the Board's job cards read them.
    monkeypatch.setattr(
        jobs_dismissed_mod, "JOBS_DISMISSED_FILE", tmp_path / "isolated-jobs-dismissed.json"
    )
    # Nor the skill fingerprints a Life OS resume records.
    from src.life_os import skill_refresh as skill_refresh_mod
    monkeypatch.setattr(
        skill_refresh_mod, "SKILL_HASHES_FILE", tmp_path / "isolated-life-os-skill-hashes.json"
    )


@pytest.fixture(autouse=True)
def _isolated_chief_pointer(request, tmp_path, monkeypatch):
    """Never read or write the real chief pointer from a test (issue #675).

    ``src.chief.pointer`` resolves ``webapp/chief-pointer.json`` off the repo
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
    from src.chief import answers as chief_answers_mod
    from src.chief import pointer as chief_pointer_mod
    monkeypatch.setattr(
        chief_pointer_mod, "CHIEF_POINTER_FILE", tmp_path / "chief-pointer.json"
    )
    # Same for the answer sheet's answered marks (#1487).
    monkeypatch.setattr(
        chief_answers_mod, "CHIEF_ANSWERED_FILE", tmp_path / "chief-answered.json"
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


def _isolate_webapp_files(tmp_path: Path, monkeypatch) -> Dict[str, Path]:
    """Point every on-disk file the webapp reads at ``tmp_path``; the paths by role."""
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
                "claude_model_efforts": {},
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
                # ...and for the chief's plan (#1279).
                "chief_plan_file": str(tmp_path / "chief-plan.json"),
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
    from src.jobs import config as jobs_cfg_mod
    from src.jobs import history as jobs_history_mod
    monkeypatch.setattr(jobs_cfg_mod, "DEFAULT_JOBS_PATH", tmp_jobs_cfg)
    # JOBS_RUNS_DIR is owned by src.jobs.history (issue #315 split) — patch
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

    _stub_quota_snapshot(monkeypatch)
    return {
        "registry": tmp_registry,
        "apps_scan_root": tmp_apps_root,
        "projects_dir": tmp_projects_dir,
        "webapp_cfg": tmp_webapp_cfg,
        "jobs_cfg": tmp_jobs_cfg,
        "jobs_runs": tmp_jobs_runs,
    }


def _stub_quota_snapshot(monkeypatch) -> None:
    """A fixed, unknown quota snapshot instead of the sibling fleet-config's."""
    # Quota API tests own the legacy rate-limits cache file; keep their canonical
    # source state deterministic even when no sibling fleet-config is checked
    # out (as in hosted CI).
    from src.usage import quota as quota_usage_mod
    monkeypatch.setattr(
        quota_usage_mod,
        "_read_snapshot",
        lambda *_args: {
            "sources": [
                {
                    "producer": "webapp-test-fixture",
                    "harness": "claude",
                    "provider": "anthropic",
                    "state": "unknown",
                    "reason": "source_absent",
                    "checked_at": None,
                    "observations": [],
                }
            ],
            "pools": [],
        },
    )


def _mock_loopback_clients(monkeypatch, voice_ocr_tts_router) -> tuple:
    """Mock the voice, OCR, TTS and LLM loopback clients the
    voice_ocr_tts router calls; ``(voice, photo_ocr, tts, llm)``."""
    # Mock the voice-transcriber loopback client (issue #165) — the
    # /api/transcribe proxy goes through it; tests assert call args and set
    # the transcript without a live voice-transcriber on :8443. Lives in
    # routers/voice_ocr_tts.py (split off routers/sessions.py, #521).
    from src.clients import voice as real_voice_client
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
    from src.clients import photo_ocr as real_photo_ocr_client
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
    from src.clients import tts as real_tts_client
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
    from src.clients import llm as real_llm_client
    llm_mock = MagicMock()
    llm_mock.summarize.return_value = "Build is green. No decision needed."
    llm_mock.LlmError = real_llm_client.LlmError
    monkeypatch.setattr(voice_ocr_tts_router, "llm_client", llm_mock)
    return voice_mock, photo_ocr_mock, tts_mock, llm_mock


def _mock_session_client(monkeypatch, routers: list) -> MagicMock:
    """One session-host client mock on every router holding its own reference."""
    from src.clients import session as real_session_client
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
    for router in routers:
        monkeypatch.setattr(router, "session_client", session_mock)
    return session_mock


def _stub_audit(monkeypatch, routers: list) -> MagicMock:
    """One audit mock on every router that writes audit lines, so no file
    lands in webapp/sessions/ during tests."""
    audit_mock = MagicMock()
    for router in routers:
        monkeypatch.setattr(router, "audit", audit_mock)
    return audit_mock


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
    paths = _isolate_webapp_files(tmp_path, monkeypatch)

    # Now import the server + routers. Important: import after monkeypatching
    # the config paths, but before patching session_client / audit (which are
    # module-level references inside each router that talks to them).
    from app.webapp import server as server_mod
    from app.webapp.routers import apps as apps_router
    from app.webapp.routers import board as board_router
    from app.webapp.routers import board_chief as board_chief_router
    from app.webapp.routers import board_spawn as board_spawn_router
    from app.webapp.routers import life_os_conversations as life_os_conversations_router
    from app.webapp.routers import life_os_files as life_os_files_router
    from app.webapp.routers import life_os_channels as life_os_channels_router
    from app.webapp.routers import life_os_spawn as life_os_spawn_router
    from app.webapp.routers import life_os_search as life_os_search_router
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
    # The Chat question card's /answer route types into a session (#1149).
    from app.webapp.routers import session_screen as session_screen_router
    session_mock = _mock_session_client(monkeypatch, [
        sessions_router, board_router, board_chief_router, board_spawn_router,
        misc_router, life_os_channels_router, session_screen_router,
    ])

    voice_mock, photo_ocr_mock, tts_mock, llm_mock = _mock_loopback_clients(monkeypatch, voice_ocr_tts_router)

    # Every router holding its own module-level `audit` reference.
    from app.webapp.routers import desktop_screen as desktop_screen_router
    from app.webapp.routers import webauthn as webauthn_router
    audit_mock = _stub_audit(monkeypatch, [
        apps_router, sessions_router, voice_ocr_tts_router, webauthn_router,
        life_os_spawn_router, life_os_conversations_router, life_os_search_router,
        life_os_files_router, board_router, board_chief_router,
        life_os_channels_router, desktop_screen_router,
    ])

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
        "tmp_registry_path": paths["registry"],
        "tmp_apps_scan_root": paths["apps_scan_root"],
        "tmp_projects_dir": paths["projects_dir"],
        "tmp_webapp_cfg_path": paths["webapp_cfg"],
        "tmp_jobs_path": paths["jobs_cfg"],
        "tmp_jobs_runs_dir": paths["jobs_runs"],
    }
    yield client, app, overrides


def _make_life_os(root: Path) -> Path:
    """Build a minimal life-os layout with one skill + identity."""
    from tests.api._life_os_support import RESUMABLE_SID

    skill = root / ".claude" / "skills" / "journal-daily"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: journal-daily\ndescription: Journal from a transcript.\n---\n# journal-daily\n",
        encoding="utf-8",
    )
    (skill / "description.md").write_text("Public blurb.", encoding="utf-8")
    (skill / "memory").mkdir()
    (skill / "memory" / "observations.md").write_text(
        "# obs\n\nprivate note", encoding="utf-8"
    )
    (skill / "conversations").mkdir()
    (skill / "conversations" / "2026-06-01-1917-trial.md").write_text(
        "trial log", encoding="utf-8"
    )
    # The placeholder that keeps an empty conversations/ tracked — must stay
    # un-deletable / un-renameable.
    (skill / "conversations" / ".gitkeep").write_text("", encoding="utf-8")
    # The digested index the Conversations view reads (#727), covering all
    # three states the UI must render: resumable, no stored session id, and a
    # non-claude agent. Deliberately written oldest-first so the endpoint's
    # newest-first ordering is a property it enforces, not one it inherits.
    (skill / "conversations" / "2026-07-02-1030-notion-schema.md").write_text(
        "notion log", encoding="utf-8"
    )
    (skill / "conversations" / "2026-08-01-0900-ferry-booking.md").write_text(
        f'<!-- capture sid="{RESUMABLE_SID}" agent="claude" updated="synthetic" -->\nferry log', encoding="utf-8"
    )
    (skill / "conversations" / "index.json").write_text(
        json.dumps([
            {
                "skill": "journal-daily",
                "file": "2026-06-01-1917-trial.md",
                "date": "2026-06-01", "slug": "trial", "turns": 4,
                "sid": "not-a-uuid", "agent": "codex",
                "topic": "a codex trial", "decisions": "none",
                "open_loops": "none",
            },
            {
                "skill": "journal-daily",
                "file": "2026-08-01-0900-ferry-booking.md",
                "date": "2026-08-01", "slug": "ferry-booking", "turns": 12,
                "sid": RESUMABLE_SID, "agent": "claude",
                "topic": "booking the ferry", "decisions": "took the 07:40",
                "open_loops": "confirm the return leg",
            },
            {
                "skill": "journal-daily",
                "file": "2026-07-02-1030-notion-schema.md",
                "date": "2026-07-02", "slug": "notion-schema", "turns": 7,
                "sid": "", "agent": "claude",
                "topic": "notion schema", "decisions": "none",
                "open_loops": "none",
                # The one-paragraph digest shape (fleet-config#1373).
                "summary": "Settled the property types for the reading list.",
            },
        ]),
        encoding="utf-8",
    )
    identity = root / "identity"
    identity.mkdir()
    (identity / "who-i-am.md").write_text("# who\n\nme", encoding="utf-8")
    return root


@pytest.fixture
def life_os_client(webapp_client, tmp_path, monkeypatch):
    """webapp_client with life_os_dir pointed at a temp life-os checkout."""
    client, app, overrides = webapp_client
    life_os = _make_life_os(tmp_path / "life-os")
    app.state.webapp_config.life_os_dir = str(life_os)
    from app.webapp.routers import life_os_conversations
    monkeypatch.setattr(life_os_conversations, "is_installed", lambda agent: True)
    # API tests own the external parser contract, so CI needs no sibling repo.
    # Native verification separately runs the real shared parser against synthetic captures.
    from src.life_os import history as life_os_history
    fleet = tmp_path / "synthetic-fleet"
    fleet.mkdir()
    app.state.webapp_config.fleet_config_dir = str(fleet)
    def synthetic_parser(fleet_dir, texts):
        if not fleet_dir.is_dir():
            raise OSError("synthetic missing contract")
        results = []
        for text in texts:
            lines = text.splitlines()
            header = lines[0] if lines and lines[0].startswith("<!-- capture ") else ""
            attrs = dict(re.findall(r'(\w+)="([^\"]*)"', header))
            results.append({"header": attrs, "body": "\n".join(lines[1:] if header else lines),
                            "native": attrs.get("agent") in ("claude", "codex") and bool(attrs.get("sid"))})
        return results
    monkeypatch.setattr(life_os_history, "_parse_capture_texts", synthetic_parser)
    overrides["life_os_dir"] = life_os
    return client, app, overrides
