"""A throwaway app-launcher with synthetic data, for ``/design-review --synthetic`` (#1227).

fleet-config's design-review walk must never attach to a real session, so a
session's Chat and Terminal views, and every surface that needs data (a
session row, a Board card, a job, a Life OS skill), can only be measured on
an instance nobody uses. This script is that instance. It speaks the
launcher contract fleet-config's ``capture.SyntheticInstance`` expects
(fleet-config#995): run with this repo's ``.venv`` from the checkout root, it
prints ``URL=<base>`` once the webapp answers, runs until its stdin reaches
EOF, then stops everything it started and deletes its temp tree.

What makes it throwaway, and synthetic only:

* **The code runs from a copy.** ``HEAD`` is exported with ``git archive``
  into a temp dir, so every ``PROJECT_ROOT``-relative file the webapp reads
  or writes (``config/jobs.json``, ``config/apps.json``, ``webapp/jobs``,
  logs, the chief pointer) is a throwaway one; the checkout is never written.
* **Every data path is synthetic.** The webapp config points
  ``projects_dir``, ``life_os_dir``, the Board state file and the rest at the
  temp tree; sibling-service URLs point at a closed loopback port; nothing
  notifies. ``USERPROFILE``/``HOME`` and ``CLAUDE_CONFIG_DIR`` point at a temp
  home, so no home-relative reader (``~/.claude/projects``, other agents'
  session folders) sees real content.
* **GitHub is synthetic too (#1286).** ``LAUNCHER_GH_CMD`` points the Board's
  GitHub refresh at ``scripts/synthetic_gh.py``, whose rows name only the
  synthetic repo, and ``github_owner`` is a synthetic owner. The real ``gh``
  keeps its login under ``%APPDATA%``, beyond the ``HOME`` redirect, so
  without this a walk's report carried the real fleet's issue titles.
* **The instance is disposable.** ``LAUNCHER_SESSION_HOST_PORT`` marks it
  non-canonical (``src/instance_role.py``), so alerts and machine-wide sweeps
  stand down, exactly as for the e2e gate's autoboot webapp.
* **The session is the gate's stub.** A session-host on a free port gets the
  e2e ``claude.cmd`` shim (``tests/e2e/stub_session.py``) and launches one
  ``--e2e-stub`` echo child; a synthetic Board state row names it by
  ``launcher_session_id`` and points Chat at a synthetic transcript.

Stdout carries only ``URL=``, ``SESSION=`` and ``ROOT=``; logging goes to
stderr and the two servers log to files under the temp tree.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.webapp.event_loop import LOOP_FACTORY  # noqa: E402
from scripts import synthetic_gh  # noqa: E402
from src.github_client import GH_CMD_ENV  # noqa: E402
from src.git_utils import run_git  # noqa: E402
from src.subprocess_flags import NO_WINDOW  # noqa: E402
from tests.e2e.stub_session import STUB_FLAG, write_claude_shim  # noqa: E402

logger = logging.getLogger("design_review_synthetic")

CLOSED_URL = "http://127.0.0.1:9"      # sibling services: a port nothing listens on
STARTUP_TIMEOUT_S = 60.0
HEARTBEAT_S = 60.0                     # keeps the synthetic Board row fresh while the walk runs
STOP_GRACE_S = 10.0
STATE_KEY = "synthetic-demo-conversation"
PROJECT_NAME = "demo-project"
SKILL_NAME = "demo-journal"


@dataclass
class Paths:
    """Everything the instance reads or writes, all under one temp root."""

    root: Path
    app: Path            # the git-archive copy the servers run from
    data: Path
    home: Path
    project: Path
    life_os: Path
    state_file: Path
    transcript: Path
    webapp_config: Path
    shim_dir: Path
    logs: Path


def _iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json(path: Path, value: object) -> None:
    _write(path, json.dumps(value, indent=2))


def export_head(dest: Path) -> Path:
    """`git archive HEAD` of this checkout, unpacked into `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    tar = dest.parent / "head.tar"
    if run_git(REPO_ROOT, ["archive", "--format=tar", "-o", str(tar), "HEAD"], timeout=120, warn_on_failure=True) is None:
        raise RuntimeError("git archive HEAD failed")
    with tarfile.open(tar) as archive:
        archive.extractall(dest, filter="data")
    tar.unlink()
    return dest


def _transcript_lines(project: Path, now: _dt.datetime) -> List[Dict[str, object]]:
    """A short Claude Code hook-JSONL conversation: prompt, a tool call and its result, a formatted reply."""
    def at(sec: int) -> str:
        return _iso(now - _dt.timedelta(minutes=10) + _dt.timedelta(seconds=sec))
    base = {"sessionId": STATE_KEY, "cwd": str(project)}
    return [
        {**base, "type": "user", "timestamp": at(0),
         "message": {"role": "user", "content": "Summarise the demo project and suggest one next step."}},
        {**base, "type": "assistant", "timestamp": at(4),
         "message": {"id": "msg-1", "role": "assistant", "content": [
             {"type": "text", "text": "I'll read the README first."},
             {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "README.md"}}]}},
        {**base, "type": "user", "timestamp": at(6),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tool-1",
              "content": "# Demo project\n\nA placeholder project for the design review."}]}},
        {**base, "type": "assistant", "timestamp": at(12),
         "message": {"id": "msg-2", "role": "assistant", "content": [
             {"type": "text", "text": (
                 "The demo project is a placeholder with a single README.\n\n"
                 "**Next steps**\n\n1. Add a first module.\n2. Add a test for it.\n\n"
                 "```python\ndef hello() -> str:\n    return \"hello\"\n```")}]}},
    ]


def seed(root: Path, app: Path, session_host_port: int, webapp_port: int) -> Paths:
    """Write the synthetic data tree, the copy's config files and the webapp config. Returns the paths."""
    data = root / "data"
    p = Paths(root=root, app=app, data=data, home=root / "home", project=data / "projects" / PROJECT_NAME,
              life_os=data / "life-os", state_file=data / "state" / "sessions-state.json",
              transcript=data / "transcripts" / f"{STATE_KEY}.jsonl",
              webapp_config=app / "config" / "webapp_config.json", shim_dir=root / "shim", logs=root / "logs")
    now = _dt.datetime.now(_dt.timezone.utc)
    for d in (p.home / ".claude", p.shim_dir, p.logs, root / "audit", root / "uploads", root / "startup"):
        d.mkdir(parents=True, exist_ok=True)

    _write(p.project / "README.md", "# Demo project\n\nA placeholder project for the design review.\n")
    _write(p.project / "webapp.bat", "@echo off\r\necho synthetic demo app\r\n")

    skill = p.life_os / ".claude" / "skills" / SKILL_NAME
    _write(skill / "SKILL.md", f"---\nname: {SKILL_NAME}\ndescription: A synthetic journal skill for the design review.\n---\n\n"
                               "# Demo journal\n\nWrite one short entry about the day.\n")
    _write(skill / "conversations" / "2026-09-01-demo-entry.md",
           "# Demo entry\n\n**You:** A calm day.\n\n**Journal:** Noted. Anything to carry into tomorrow?\n")
    _json(skill / "conversations" / "index.json", [
        {"file": "2026-09-01-demo-entry.md", "date": "2026-09-01", "turns": 2, "title": "Demo entry",
         "summary": "A short synthetic journal entry."}])
    _write(p.life_os / "identity" / "profile.md", "# Profile\n\nA synthetic profile for the design review.\n")

    _write(p.transcript, "".join(json.dumps(line) + "\n" for line in _transcript_lines(p.project, now)))
    _json(p.state_file, {})
    # A chief's plan (#1279), so the Board's card has rows to review. No chief runs here, so the card
    # also shows its "chief not running" line.
    _json(data / "state" / "chief-plan.json", {
        "version": 1, "updated_at": _iso(now - _dt.timedelta(minutes=12)),
        "lanes": [{"repo": PROJECT_NAME, "session": "", "item": "#12", "status": "building"}],
        "queue": [
            {"repo": PROJECT_NAME, "ref": "#12", "title": "Synthetic item in progress", "status": "building", "note": ""},
            {"repo": PROJECT_NAME, "ref": "#13", "title": "Synthetic item queued next", "status": "queued",
             "note": "after #12"}],
        "waiting_on_roberto": [{"text": "A synthetic decision", "ref": f"{PROJECT_NAME}#14"}]})

    # A schedule so the row shows its Pause item, hence its ⋮ menu. Display only: a Windows task is
    # registered only by the jobs API's write routes (`sync_schtasks`), which the walk never calls.
    _json(app / "config" / "jobs.json", {"jobs": [{
        "id": "synthetic-weekly-report", "name": "Weekly report (synthetic)",
        "script_path": str(data / "jobs" / "weekly_report.py"), "args": "",
        "schedule": {"type": "weekly", "day": "MON", "at": "09:00"}, "visible": True}]})
    _write(data / "jobs" / "weekly_report.py", '"""A synthetic job script; the design review never runs it."""\n')
    _json(app / "config" / "apps.json", {"scan_root": str(data / "projects"), "apps": [{
        "id": "demo-project-webapp", "name": "Demo webapp", "kind": "webapp",
        "bat_path": str(p.project / "webapp.bat"), "added_at": "2026-09-01T00:00:00"}]})
    _json(p.webapp_config, {
        "host": "127.0.0.1", "port": webapp_port, "session_host_port": session_host_port,
        "auth_token": secrets.token_urlsafe(24),
        "projects_dir": str(data / "projects"), "apps_scan_root": str(data / "projects"), "projects_ignore": [],
        "life_os_dir": str(p.life_os), "claude_config_dir": str(p.home / ".claude"),
        "sessions_state_file": str(p.state_file), "rate_limits_file": str(data / "state" / "rate-limits.json"),
        "chief_plan_file": str(data / "state" / "chief-plan.json"),
        "context_filter_mode_file": str(data / "state" / "context-filter-mode.json"),
        "context_filter_log_file": str(data / "state" / "context-filter-shadow.jsonl"),
        "voice_transcriber_url": CLOSED_URL, "photo_ocr_url": CLOSED_URL, "llm_hub_url": CLOSED_URL,
        "claude_show_local_window": False, "notify_on_failure": False, "github_owner": synthetic_gh.OWNER,
        "telegram_bot_token": "", "telegram_chat_id": "", "pushover_api_token": "", "pushover_user_key": "",
    })
    write_claude_shim(p.shim_dir)
    return p


def state_rows(p: Paths, session_id: str) -> Dict[str, Dict[str, object]]:
    """The Board state file's content: one row naming the stub session exactly (`launcher_session_id`)."""
    return {STATE_KEY: {
        "project": PROJECT_NAME, "status": "working", "agent": "claude", "cwd": str(p.project),
        "transcript_path": str(p.transcript), "launcher_session_id": session_id,
        "updated_at": _iso(_dt.datetime.now(_dt.timezone.utc)),
    }}


def isolated_env(p: Paths, session_host_port: int) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({
        "USERPROFILE": str(p.home), "HOME": str(p.home), "CLAUDE_CONFIG_DIR": str(p.home / ".claude"),
        "LAUNCHER_SESSION_HOST_PORT": str(session_host_port), "LAUNCHER_WEBAPP_CONFIG": str(p.webapp_config),
        "LAUNCHER_AUDIT_DIR": str(p.root / "audit"), "LAUNCHER_UPLOAD_ROOT": str(p.root / "uploads"),
        "LAUNCHER_STARTUP_DIR": str(p.root / "startup"), "PYTHONUTF8": "1",
        # The Board's GitHub refresh runs the fake gh, never the real one (#1286): the real CLI's login lives
        # under %APPDATA%, which the HOME redirect above does not reach, so it read the real fleet's backlog.
        # GH_CONFIG_DIR is the second guard: a real gh reached any other way finds no login here.
        GH_CMD_ENV: json.dumps([sys.executable, str(p.app / "scripts" / "synthetic_gh.py")]),
        "GH_CONFIG_DIR": str(p.home / "gh"),
    })
    return env


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn(argv: List[str], cwd: Path, env: Dict[str, str], log_path: Path) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, creationflags=NO_WINDOW)


def _wait(check, what: str, proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{what} exited ({proc.returncode}) before it was ready")
        if check():
            return
        time.sleep(0.5)
    raise RuntimeError(f"{what} not ready within {timeout:g}s")


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=2) as res:
            return res.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _post(url: str, body: Dict[str, object], timeout: float) -> Dict[str, object]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8") or "{}")


def launch_stub_session(session_host_port: int, project: Path) -> str:
    """One `--e2e-stub` PTY session on the disposable session-host, as the e2e fixture launches it."""
    res = _post(f"http://127.0.0.1:{session_host_port}/sessions",
                {"project_dir": str(project), "name": PROJECT_NAME, "flags": STUB_FLAG, "agent": "claude"}, timeout=60)
    sid = str(res.get("session_id") or "")
    if not sid:
        raise RuntimeError(f"stub session launch returned no session_id: {str(res)[:200]}")
    return sid


def stop_process(proc: subprocess.Popen, what: str) -> None:
    """Terminate one server this script started; its process tree only if it does not exit."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    logger.warning("⚠️ %s (pid %s) did not stop; killing its process tree", what, proc.pid)
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()
    try:
        proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        logger.error("❌ %s (pid %s) is still running after a tree kill", what, proc.pid)


def remove_tree(root: Path) -> bool:
    """Delete the temp tree; a file a just-exited child still holds gets a few seconds."""
    for _ in range(10):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            return True
        time.sleep(1)
    logger.warning("⚠️ could not remove %s entirely", root)
    return False


def run() -> int:
    root = Path(tempfile.mkdtemp(prefix="al-design-synthetic-"))
    procs: List[tuple] = []
    session_host_port = sid = None
    try:
        app = export_head(root / "app")
        session_host_port, webapp_port = free_port(), free_port()
        p = seed(root, app, session_host_port, webapp_port)
        env = isolated_env(p, session_host_port)

        host_env = {**env, "PATH": f"{p.shim_dir}{os.pathsep}{env.get('PATH', '')}"}
        host = _spawn([sys.executable, str(app / "launcher.py"), "session-host", "--port", str(session_host_port)],
                      app, host_env, p.logs / "session-host.log")
        procs.append((host, "session-host"))
        _wait(lambda: _listening(session_host_port), "session-host", host)
        sid = launch_stub_session(session_host_port, p.project)
        _json(p.state_file, state_rows(p, sid))
        logger.info("ℹ️ stub session %s on the session-host at :%s", sid, session_host_port)

        webapp = _spawn([sys.executable, "-m", "uvicorn", "app.webapp.server:app", "--host", "127.0.0.1",
                         "--port", str(webapp_port), "--log-level", "warning", "--loop", LOOP_FACTORY],
                        app, env, p.logs / "webapp.log")
        procs.append((webapp, "webapp"))
        base = f"http://127.0.0.1:{webapp_port}"
        _wait(lambda: _healthy(base), "webapp", webapp)

        print(f"ROOT={root}", flush=True)
        print(f"SESSION={sid}", flush=True)
        print(f"URL={base}", flush=True)
        logger.info("✅ synthetic instance ready at %s; stops when stdin closes", base)

        closed = threading.Event()
        threading.Thread(target=lambda: (sys.stdin.read(), closed.set()), daemon=True).start()
        while not closed.wait(HEARTBEAT_S):
            _json(p.state_file, state_rows(p, sid))
        return 0
    except Exception as exc:  # noqa: BLE001 — every failure is reported, then cleaned up below
        logger.error("❌ synthetic instance failed: %s", exc)
        return 1
    finally:
        if sid and session_host_port:
            try:
                _post(f"http://127.0.0.1:{session_host_port}/sessions/{sid}/stop", {}, timeout=15)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                logger.warning("⚠️ could not stop the stub session %s: %s", sid, exc)
        for proc, what in reversed(procs):
            stop_process(proc, what)
        if remove_tree(root):
            logger.info("ℹ️ removed %s", root)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
