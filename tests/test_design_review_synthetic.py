"""The design-review synthetic instance (#1227): synthetic data only, and it cleans up after itself.

``scripts/design_review_synthetic.py`` boots a throwaway app-launcher that
fleet-config's ``/design-review --synthetic`` walks. The pure tests pin that
everything it seeds is synthetic and self-consistent: every config path lives
under its temp root, the Life OS skill, job and app resolve through the
production readers, and the Board state row joins the stub session exactly,
so Chat renders the synthetic transcript. The integration test boots the
real thing once (Windows only: the stub shim is a ``.cmd``) and checks the
launcher contract end to end, including that closing stdin stops the
servers and deletes the temp tree.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from scripts import design_review_synthetic as syn
from src import board_sessions
from src.jobs_config import load_jobs
from src.scanner import scan_skills
from src.session_transcript import claude_entries
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parent.parent
PATH_FIELDS = ("projects_dir", "apps_scan_root", "life_os_dir", "claude_config_dir", "sessions_state_file",
               "rate_limits_file", "chief_plan_file", "context_filter_mode_file", "context_filter_log_file")


def _seed(tmp_path: Path) -> syn.Paths:
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    return syn.seed(tmp_path, app, session_host_port=50001, webapp_port=50002)


def _lines(path: Path):
    raw, pos, out = path.read_bytes(), 0, []
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            out.append((pos, chunk.decode("utf-8")))
        pos += len(chunk) + 1
    return out


def test_every_data_path_is_under_the_temp_root(tmp_path: Path) -> None:
    p = _seed(tmp_path)
    cfg = json.loads(p.webapp_config.read_text(encoding="utf-8"))
    root = tmp_path.resolve()
    for field in PATH_FIELDS:
        assert root in Path(cfg[field]).resolve().parents, f"{field} escapes the temp root: {cfg[field]}"
    assert cfg["session_host_port"] == 50001 and cfg["port"] == 50002 and cfg["host"] == "127.0.0.1"
    assert {cfg["voice_transcriber_url"], cfg["photo_ocr_url"], cfg["llm_hub_url"]} == {syn.CLOSED_URL}
    assert not cfg["notify_on_failure"] and not cfg["telegram_bot_token"] and not cfg["claude_show_local_window"]
    env = syn.isolated_env(p, 50001)
    for key in ("USERPROFILE", "HOME", "CLAUDE_CONFIG_DIR", "LAUNCHER_AUDIT_DIR", "LAUNCHER_UPLOAD_ROOT",
                "LAUNCHER_STARTUP_DIR", "LAUNCHER_WEBAPP_CONFIG"):
        assert root in Path(env[key]).resolve().parents, f"{key} escapes the temp root: {env[key]}"
    assert env["LAUNCHER_SESSION_HOST_PORT"] == "50001"  # marks the instance disposable (src/instance_role.py)
    jobs = json.loads((p.app / "config" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    apps = json.loads((p.app / "config" / "apps.json").read_text(encoding="utf-8"))["apps"]
    assert all(root in Path(j["script_path"]).resolve().parents for j in jobs)
    assert all(root in Path(a["bat_path"]).resolve().parents for a in apps)


def test_seeded_data_resolves_through_the_production_readers(tmp_path: Path) -> None:
    p = _seed(tmp_path)
    assert [s.name for s in scan_skills(p.life_os)] == [syn.SKILL_NAME]
    jobs = load_jobs(p.app / "config" / "jobs.json").jobs
    assert [j.id for j in jobs] == ["synthetic-weekly-report"] and jobs[0].schedule.type == "weekly"
    kinds = [e["kind"] for e in claude_entries(_lines(p.transcript))]
    assert kinds == ["user", "assistant", "tool_call", "assistant"], kinds


def test_state_row_joins_the_stub_session_exactly(tmp_path: Path) -> None:
    p = _seed(tmp_path)
    live = [{"session_id": "stub-sid", "kind": "pty", "agent": "claude", "project_dir": str(p.project),
             "name": syn.PROJECT_NAME, "alive": True, "started_at": "2026-09-24T10:00:00Z"}]
    row = board_sessions.state_row_for_session(live, syn.state_rows(p, "stub-sid"), "stub-sid")
    assert row is not None and row["transcript_path"] == str(p.transcript)
    assert board_sessions.state_row_for_session(live, syn.state_rows(p, "other-sid"), "stub-sid") is None


@pytest.mark.skipif(sys.platform != "win32", reason="the stub session's claude shim is a .cmd")
def test_launcher_contract_end_to_end() -> None:
    proc = subprocess.Popen([sys.executable, "scripts/design_review_synthetic.py"], cwd=REPO_ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            encoding="utf-8", creationflags=NO_WINDOW)
    try:
        lines = {}
        while "URL" not in lines:
            line = proc.stdout.readline()
            assert line, f"launcher exited ({proc.wait()}) before printing URL="
            key, _, value = line.strip().partition("=")
            lines[key] = value

        def get(path: str) -> dict:
            with urllib.request.urlopen(lines["URL"] + path, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))

        sid = lines["SESSION"]
        assert [s["session_id"] for s in get("/api/claude-code/sessions")["sessions"]] == [sid]
        assert [e["kind"] for e in get(f"/api/claude-code/sessions/{sid}/transcript")["entries"]] == [
            "user", "assistant", "tool_call", "assistant"]
        assert sid in json.dumps(get("/api/board"))
        assert [s["name"] for s in get("/api/life-os/skills")["skills"]] == [syn.SKILL_NAME]
        assert [j["id"] for j in get("/api/jobs")["jobs"]] == ["synthetic-weekly-report"]
    finally:
        proc.stdin.close()
        rc = proc.wait(timeout=120)
    assert rc == 0
    assert not Path(lines["ROOT"]).exists(), "the temp tree must be gone once stdin closes"
