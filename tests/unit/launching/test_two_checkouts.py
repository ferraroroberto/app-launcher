"""Two checkouts on one port: only a process running from this checkout counts as ours.

A second clone at the same commit answers ``/healthz`` and ``/api/version`` exactly like this one,
so the restart check and the webapp adoption both have to ask which tree the answering process
runs from. The servers here are real processes started with their own working directory, which is
how the tray starts a webapp.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

import psutil
import pytest

from app.webapp.manager import PROJECT_ROOT, WebappManager, WebappManagerConfig
from src.launching import restart_check
from src.subprocess_flags import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD = "86224f1b"
BAT_TIMEOUT_S = 60

_SERVER = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"git_sha": sys.argv[2], "pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

srv = HTTPServer(("127.0.0.1", int(sys.argv[1])), H)
print(os.getpid(), flush=True)
srv.serve_forever()
"""

_LISTENER = """
import os, socket, sys, time
s = socket.socket()
s.bind((sys.argv[1], int(sys.argv[2])))
s.listen(1)
print(os.getpid(), flush=True)
time.sleep(60)
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator:
    """``spawn(source, args, cwd)`` runs a throwaway script in ``cwd`` and returns its pid."""
    procs: List["subprocess.Popen[str]"] = []

    def _start(source: str, args: List[str], cwd: Path) -> int:
        cwd.mkdir(parents=True, exist_ok=True)
        script = tmp_path / f"stand_in_{len(procs)}.py"
        script.write_text(source, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(script), *args],
            cwd=str(cwd), stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
        )
        procs.append(proc)
        assert proc.stdout is not None
        return int(proc.stdout.readline())

    yield _start
    for proc in procs:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def serve(spawn) -> Iterator:
    """``serve(cwd)`` starts a webapp stand-in in ``cwd``; returns ``(port, pid)``."""

    def _start(cwd: Path) -> Tuple[int, int]:
        port = _free_port()
        return port, spawn(_SERVER, [str(port), HEAD], cwd)

    yield _start


@pytest.fixture
def files(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(restart_check, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(restart_check, "RESTART_LOG", tmp_path / "restart.log")
    monkeypatch.setattr(restart_check, "resolve_git_sha", lambda: HEAD)
    return tmp_path


def _manager(port: int) -> WebappManager:
    return WebappManager(WebappManagerConfig(host="127.0.0.1", port=port, startup_timeout_seconds=2.0))


class TestRestartVerify:
    def _verify(self, files: Path, port: int) -> Tuple[int, str]:
        (files / "baseline.json").write_text(f'{{"port": {port}, "pid": null}}', encoding="utf-8")
        code = restart_check.verify(port, timeout_s=0)
        return code, (files / "restart.log").read_text(encoding="utf-8")

    def test_a_new_pid_at_head_from_another_checkout_fails(self, tmp_path, files, serve) -> None:
        other = tmp_path / "other"
        port, pid = serve(other)
        code, log = self._verify(files, port)
        assert code == 1, log
        assert f"pid {pid}" in log and "another checkout" in log and str(other.resolve()) in log

    def test_the_same_answer_from_this_checkout_passes(self, files, serve) -> None:
        port, pid = serve(PROJECT_ROOT)
        code, log = self._verify(files, port)
        assert code == 0, log
        assert f"pid {pid}" in log


class TestAdoption:
    def test_another_checkouts_webapp_is_refused_not_adopted(self, tmp_path, serve, monkeypatch) -> None:
        import app.webapp.manager as manager_mod

        port, pid = serve(tmp_path / "other")
        spawned: List[object] = []
        monkeypatch.setattr(manager_mod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
        with pytest.raises(RuntimeError) as exc:
            _manager(port).start(wait=False)
        message = str(exc.value)
        assert f"pid {pid} from another checkout" in message and "not adopted" in message
        assert not spawned

    def test_this_checkouts_webapp_is_adopted(self, serve) -> None:
        port, _pid = serve(PROJECT_ROOT)
        assert _manager(port).start(wait=False).running is True

    def test_a_listener_on_another_address_does_not_block_adoption(self, tmp_path, serve, spawn) -> None:
        # Tailscale listens on the same port number on the tailnet addresses only.
        host = next(
            (a.address for addrs in psutil.net_if_addrs().values() for a in addrs
             if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254."))),
            None,
        )
        if host is None:
            pytest.skip("no non-loopback IPv4 address on this machine")
        port, _pid = serve(PROJECT_ROOT)
        spawn(_LISTENER, [host, str(port)], tmp_path / "elsewhere")
        assert _manager(port).start(wait=False).running is True


class TestSessionHostAdoption:
    def _start(self, monkeypatch, port: int) -> Tuple[str, List[object], List[str]]:
        from app.tray import tray

        spawned: List[object] = []
        notes: List[str] = []
        monkeypatch.setattr(tray, "_session_host_port", lambda: port)
        monkeypatch.setattr(tray, "_notify", lambda title, _msg: notes.append(title))
        monkeypatch.setattr(tray.subprocess, "Popen", lambda cmd, **_kw: spawned.append(cmd))
        outcome = tray.TrayApp.__new__(tray.TrayApp)._start_session_host()
        return outcome, spawned, notes

    def test_a_host_from_another_checkout_is_named_not_adopted(self, tmp_path, serve, monkeypatch) -> None:
        port, pid = serve(tmp_path / "other")
        outcome, spawned, notes = self._start(monkeypatch, port)
        assert outcome.startswith(f"refused the session-host on :{port}") and f"pid {pid}" in outcome
        assert notes and not spawned

    def test_this_checkouts_host_is_adopted(self, serve, monkeypatch) -> None:
        port, _pid = serve(PROJECT_ROOT)
        outcome, spawned, notes = self._start(monkeypatch, port)
        assert outcome == f"adopted the session-host already listening on :{port}"
        assert not spawned and not notes


class TestPreflightAndOwner:
    """The two checks ``tray.bat --restart`` runs around the shared lifecycle helper."""

    def test_preflight_refuses_another_checkouts_holder_and_names_it(self, tmp_path, files, serve) -> None:
        other = tmp_path / "other"
        port, pid = serve(other)
        assert restart_check.main(["preflight", "--port", str(port)]) == 1
        log = (files / "restart.log").read_text(encoding="utf-8")
        assert "restart refused" in log and f"pid {pid}" in log and str(other.resolve()) in log

    def test_preflight_lets_this_checkout_and_a_free_port_through(self, files, serve) -> None:
        port, _pid = serve(PROJECT_ROOT)
        assert restart_check.main(["preflight", "--port", str(port)]) == 0
        assert restart_check.main(["preflight", "--port", str(_free_port())]) == 0

    def test_owner_passes_only_for_this_checkout(self, tmp_path, files, serve) -> None:
        mine, _ = serve(PROJECT_ROOT)
        theirs, _ = serve(tmp_path / "other")
        assert restart_check.main(["owner", "--port", str(mine)]) == 0
        assert restart_check.main(["owner", "--port", str(theirs)]) == 1
        assert restart_check.main(["owner", "--port", str(_free_port())]) == 1


_STUB_HELPER = """\
Set-Content -Path $env:TRAY_STUB_OUT -Value 'ran' -Encoding ASCII
exit 0
"""

# Stands for restart_check inside the throwaway repo: each action exits with its own env code.
_STUB_CHECK = """\
import os, sys
sys.exit(int(os.environ.get("STUB_" + sys.argv[1].upper(), "0")))
"""


@pytest.mark.windows_only
@pytest.mark.skipif(sys.platform != "win32", reason="tray.bat is Windows-only")
class TestTrayBatRestart:
    """A throwaway copy of tray.bat with a stub lifecycle helper and a stub check module."""

    def _run(self, tmp_path: Path, *args: str, **codes: str) -> Tuple[int, bool]:
        repo = tmp_path / "repo"
        (repo / "src" / "launching").mkdir(parents=True)
        shutil.copyfile(REPO_ROOT / "tray.bat", repo / "tray.bat")
        (repo / "src" / "__init__.py").write_text("", encoding="ascii")
        (repo / "src" / "launching" / "__init__.py").write_text("", encoding="ascii")
        (repo / "src" / "launching" / "restart_check.py").write_text(_STUB_CHECK, encoding="ascii")
        import _winapi

        _winapi.CreateJunction(str(Path(sys.prefix)), str(repo / ".venv"))
        home = tmp_path / "home"
        stub = home / ".claude" / "tray" / "tray_lifecycle.ps1"
        stub.parent.mkdir(parents=True)
        stub.write_text(_STUB_HELPER, encoding="ascii")
        out = tmp_path / "helper_ran.txt"
        env = {**os.environ, "USERPROFILE": str(home), "TRAY_STUB_OUT": str(out), **codes}
        try:
            done = subprocess.run(
                ["cmd.exe", "/d", "/c", str(repo / "tray.bat"), *args],
                env=env, capture_output=True, timeout=BAT_TIMEOUT_S, creationflags=NO_WINDOW,
            )
        finally:
            os.rmdir(repo / ".venv")  # drop the junction only, never what it points at
        return done.returncode, out.exists()

    def test_a_refused_preflight_stops_before_the_helper(self, tmp_path) -> None:
        code, helper_ran = self._run(tmp_path, "--restart", STUB_PREFLIGHT="1", STUB_OWNER="0")
        assert code == 1 and not helper_ran

    def test_a_foreign_owner_after_the_helper_fails_the_restart(self, tmp_path) -> None:
        code, helper_ran = self._run(tmp_path, "--restart", STUB_PREFLIGHT="0", STUB_OWNER="1")
        assert code == 1 and helper_ran

    def test_a_clean_restart_and_a_plain_start_pass(self, tmp_path) -> None:
        assert self._run(tmp_path / "a", "--restart", STUB_PREFLIGHT="0", STUB_OWNER="0") == (0, True)
        # A plain start runs neither check.
        assert self._run(tmp_path / "b", STUB_PREFLIGHT="1", STUB_OWNER="1") == (0, True)
