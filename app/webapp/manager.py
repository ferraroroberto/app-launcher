"""Webapp process manager — adopt-or-spawn for uvicorn.

Mirrors photo-ocr's manager: status() probes /healthz and the TCP port,
start() adopts an already-listening uvicorn or spawns one, stop() only
terminates a process this manager owns.

Health is a THREE-state fact, not a boolean (#1005). A wedged uvicorn still
LISTENs, so "the port accepts a connection" cannot establish that the webapp
is serving - it only rules out "nothing is there". Folding that into
``running=True`` is what let start() adopt a wedge and report it ready, and
made restart() refuse it with the wrong reason ("started externally"); the
sibling ``app/tray/watchdog.py`` exists because that conflation hid the #386
wedge for hours. So ``WebappStatus.health`` reports ``answering``,
``bound_not_answering`` (honestly unknown - a connection succeeded and
/healthz did not answer) or ``down``, ``running`` is True only for
``answering``, and every caller branches on ``health`` explicitly rather than
testing ``running`` for truth.

Used by the tray so launching `tray.bat` brings the webapp up.
Standalone `webapp.bat` is the "server only, no tray" alternative.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from app.tray.single_instance import cross_process_lock
from app.webapp.event_loop import LOOP_FACTORY
from src.subprocess_flags import NO_WINDOW, NO_WINDOW_NEW_GROUP

logger = logging.getLogger(__name__)

OWNERSHIP_NONE = "none"
OWNERSHIP_OURS = "ours"
OWNERSHIP_EXTERNAL = "external"

# `WebappStatus.health` - what a probe could actually establish.
HEALTH_ANSWERING = "answering"            # /healthz returned 200
HEALTH_BOUND_NOT_ANSWERING = "bound_not_answering"  # port accepts, /healthz does not
HEALTH_DOWN = "down"                      # nothing is listening

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class WebappManagerConfig:
    """Runtime knobs read from config/config.json's `webapp` section."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8445
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 1.0
    poll_interval_seconds: float = 0.4


@dataclass
class WebappStatus:
    # True only when /healthz answered. "Bound but not answering" is NOT
    # running - read `health` for that, never `running` alone.
    running: bool
    ownership: str
    health: str
    pid: Optional[int]
    port: int
    base_url: str
    detail: str


def load_config(raw: Optional[Dict[str, Any]] = None) -> WebappManagerConfig:
    raw = raw or {}
    return WebappManagerConfig(
        enabled=bool(raw.get("enabled", True)),
        host=str(raw.get("host", "0.0.0.0")),
        port=int(raw.get("port", 8445)),
        startup_timeout_seconds=float(raw.get("startup_timeout_seconds", 15.0)),
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 1.0)),
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 0.4)),
    )


def cert_paths(project_root: Optional[Path] = None) -> Optional[tuple[Path, Path]]:
    root = project_root or PROJECT_ROOT
    cert = root / "webapp" / "certificates" / "cert.pem"
    key = root / "webapp" / "certificates" / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    return None


def _probe_url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"


def check_tailscale_cert() -> None:
    """Auto-renew a Tailscale cert expiring within 30 days, before uvicorn
    binds (project-scaffolding#89). No-op on a self-signed cert or when no
    cert exists; best-effort — a cert problem must never block startup.
    """
    script = PROJECT_ROOT / "scripts" / "gen_tailscale_cert.py"
    if not script.exists() or cert_paths() is None:
        return
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--check"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(PROJECT_ROOT),
            creationflags=NO_WINDOW,
        )
        out = (result.stdout or "").strip()
        if out:
            logger.info(f"🔐 tailscale cert check: {out}")
    except Exception as exc:
        logger.warning(f"⚠️  tailscale cert check failed (ignored): {exc}")


class WebappManager:
    def __init__(self, config: Optional[WebappManagerConfig] = None) -> None:
        self.config = config or WebappManagerConfig()
        self._proc: Optional[subprocess.Popen] = None
        self._session = requests.Session()
        self._session.verify = False
        try:
            from urllib3.exceptions import InsecureRequestWarning
            import urllib3

            urllib3.disable_warnings(InsecureRequestWarning)
        except Exception:
            pass

    @property
    def base_url(self) -> str:
        scheme = "https" if cert_paths() else "http"
        return _probe_url(scheme, self.config.host, self.config.port)

    def is_reachable(self) -> bool:
        for scheme in ("https", "http"):
            url = _probe_url(scheme, self.config.host, self.config.port) + "/healthz"
            try:
                r = self._session.get(url, timeout=self.config.request_timeout_seconds)
                if r.status_code == 200:
                    return True
            except requests.RequestException:
                continue
        return False

    def is_port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            host = self.config.host if self.config.host != "0.0.0.0" else "127.0.0.1"
            return s.connect_ex((host, self.config.port)) == 0

    def probe_health(self) -> str:
        """What a probe can establish right now - never more than that.

        A successful TCP connect is evidence that *something* holds the
        port, and nothing more; only a /healthz round-trip shows the webapp
        is serving. The middle state is reported as itself.
        """
        if self.is_reachable():
            return HEALTH_ANSWERING
        if self.is_port_in_use():
            return HEALTH_BOUND_NOT_ANSWERING
        return HEALTH_DOWN

    def status(self) -> WebappStatus:
        running_here = self._proc is not None and self._proc.poll() is None
        health = self.probe_health()

        if health == HEALTH_ANSWERING:
            if running_here:
                return WebappStatus(
                    running=True,
                    ownership=OWNERSHIP_OURS,
                    health=health,
                    pid=self._proc.pid,
                    port=self.config.port,
                    base_url=self.base_url,
                    detail="running (started by this process)",
                )
            return WebappStatus(
                running=True,
                ownership=OWNERSHIP_EXTERNAL,
                health=health,
                pid=None,
                port=self.config.port,
                base_url=self.base_url,
                detail="running (external — adopted)",
            )
        if health == HEALTH_BOUND_NOT_ANSWERING:
            # Deliberately NOT running: health could not be established, and
            # an unresolved probe is its own state, never a pass. Ownership is
            # still known - a wedge we spawned is ours to stop and restart,
            # one we did not is not ours to touch.
            return WebappStatus(
                running=False,
                ownership=OWNERSHIP_OURS if running_here else OWNERSHIP_EXTERNAL,
                health=health,
                pid=self._proc.pid if running_here else None,
                port=self.config.port,
                base_url=self.base_url,
                detail=(
                    f"bound on :{self.config.port} but not answering /healthz "
                    "— health unknown"
                ),
            )
        return WebappStatus(
            running=False,
            ownership=OWNERSHIP_NONE,
            health=HEALTH_DOWN,
            pid=None,
            port=self.config.port,
            base_url=self.base_url,
            detail="not running",
        )

    def start(self, wait: bool = True) -> WebappStatus:
        if not self.config.enabled:
            logger.info("ℹ️  Webapp is disabled in config (webapp.enabled=false)")
            return self.status()

        # Race-safe adopt-or-spawn (project-scaffolding#39): serialize the
        # status()-then-Popen critical section across processes so two trays
        # starting at once cannot both spawn uvicorn. The loser blocks, then
        # re-checks below and adopts the now-listening webapp. The lock is held
        # through _wait_until_ready so a serialized caller sees a bound port.
        # cross_process_lock fails open (Windows mutex glitch / non-Windows), so
        # it never blocks startup. Vendored byte-identical from the scaffold.
        with cross_process_lock(rf"Global\app-launcher-webapp-start-{self.config.port}"):
            current = self.status()
            if current.health == HEALTH_BOUND_NOT_ANSWERING:
                # Something holds the port and will not say whether it is
                # serving. Adopting it would report a wedge as "ready";
                # spawning over it would just fail to bind. Give it the same
                # budget a fresh spawn gets, then say exactly what is unknown.
                current = self._await_answer()
            if current.health == HEALTH_BOUND_NOT_ANSWERING:
                raise RuntimeError(
                    f"❌ webapp on :{self.config.port} is bound but did not answer "
                    f"/healthz within {self.config.startup_timeout_seconds}s"
                    + (
                        " (this process spawned it)"
                        if current.ownership == OWNERSHIP_OURS
                        else ""
                    )
                    + " — health could not be established, so it was neither "
                    "adopted nor replaced. Restart with tray.bat --restart."
                )
            if current.running and current.ownership == OWNERSHIP_OURS:
                logger.info(f"ℹ️  Webapp already {current.detail}")
                return current
            if current.running:
                logger.info(f"🔗 Adopting external webapp at {current.base_url}")
                return current

            check_tailscale_cert()
            cmd = self._build_command()
            logger.info(f"🚀 Starting webapp: {' '.join(cmd)}")

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            try:
                popen_kwargs: Dict[str, Any] = dict(
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    creationflags=NO_WINDOW_NEW_GROUP,
                )
                self._proc = subprocess.Popen(cmd, **popen_kwargs)
            except FileNotFoundError as exc:
                raise RuntimeError(f"❌ python launcher not found: {exc}") from exc
            except Exception as exc:
                raise RuntimeError(f"❌ failed to launch webapp: {exc}") from exc

            if wait:
                self._wait_until_ready()
            return self.status()

    def restart(self, wait: bool = True) -> WebappStatus:
        status = self.status()
        # Distinct conditions get distinct messages: "someone else's healthy
        # webapp" and "a wedge nobody here owns" are different problems with
        # different remedies, and the old code reported both as the first.
        if (
            status.health == HEALTH_BOUND_NOT_ANSWERING
            and status.ownership == OWNERSHIP_EXTERNAL
        ):
            raise RuntimeError(
                f"❌ webapp on :{self.config.port} is bound but not answering /healthz, "
                "and this process did not start it — health could not be established, "
                "so it was not restarted. Reclaim the port with tray.bat --restart."
            )
        if status.running and status.ownership == OWNERSHIP_EXTERNAL:
            raise RuntimeError(
                "Webapp is running but was started externally — cannot restart from here"
            )
        # Ownership, not `running`: a wedge we spawned is not running by the
        # honest definition, and still has to be stopped before the respawn.
        if status.ownership == OWNERSHIP_OURS:
            self.stop()
        return self.start(wait=wait)

    def stop(self) -> WebappStatus:
        status = self.status()
        if status.ownership == OWNERSHIP_EXTERNAL:
            if status.health == HEALTH_BOUND_NOT_ANSWERING:
                logger.info(
                    f"✋ Leaving the external process on :{self.config.port} alone "
                    "(bound, not answering — not ours to kill)"
                )
            else:
                logger.info("✋ Leaving external webapp running (not ours)")
            return status
        # `self._proc`, not `status.running`: a process we spawned that has
        # stopped answering still has to be terminated, and that is exactly
        # the case `running` no longer covers.
        if self._proc is None:
            return status

        p = self._proc
        logger.info(f"🛑 Stopping webapp (pid={p.pid})")
        try:
            if sys.platform == "win32":
                try:
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception as exc:
                    logger.debug(f"CTRL_BREAK_EVENT failed: {exc}")
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
        finally:
            self._proc = None

        # Re-probe rather than assert: our process is gone, but something
        # else may still hold the port, and "stopped" would be a claim this
        # method never checked - the same shape of lie the tri-state fixes.
        after = self.status()
        if after.health != HEALTH_DOWN:
            return after
        return WebappStatus(
            running=False,
            ownership=OWNERSHIP_NONE,
            health=HEALTH_DOWN,
            pid=None,
            port=self.config.port,
            base_url=self.base_url,
            detail="stopped",
        )

    def _build_command(self) -> List[str]:
        py = sys.executable
        cmd: List[str] = [
            py,
            "-m",
            "uvicorn",
            "app.webapp.server:app",
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--log-level",
            "warning",
            "--loop",
            LOOP_FACTORY,
        ]
        certs = cert_paths()
        if certs is not None:
            cert, key = certs
            cmd.extend(
                [
                    "--ssl-keyfile",
                    str(key),
                    "--ssl-certfile",
                    str(cert),
                ]
            )
        return cmd

    def _await_answer(self) -> WebappStatus:
        """Re-probe a bound-but-silent port for the startup budget.

        A uvicorn that has bound and not yet finished booting is
        indistinguishable from a wedged one at a single instant, so the
        middle state gets the same grace a fresh spawn would - and is
        reported unresolved only once that budget is spent.
        """
        deadline = time.time() + self.config.startup_timeout_seconds
        while time.time() < deadline:
            time.sleep(self.config.poll_interval_seconds)
            status = self.status()
            if status.health != HEALTH_BOUND_NOT_ANSWERING:
                return status
        return self.status()

    def _wait_until_ready(self) -> None:
        deadline = time.time() + self.config.startup_timeout_seconds
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                raise RuntimeError("❌ webapp uvicorn exited before becoming ready")
            if self.is_reachable():
                logger.info(f"✅ Webapp ready at {self.base_url}")
                return
            time.sleep(self.config.poll_interval_seconds)
        raise RuntimeError(
            f"❌ webapp did not become ready within "
            f"{self.config.startup_timeout_seconds}s"
        )
