"""Webapp process manager — adopt-or-spawn for uvicorn.

Mirrors photo-ocr's manager: status() probes /healthz + a TCP fallback,
start() adopts an already-listening uvicorn or spawns one, stop() only
terminates a process this manager owns.

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

logger = logging.getLogger(__name__)

OWNERSHIP_NONE = "none"
OWNERSHIP_OURS = "ours"
OWNERSHIP_EXTERNAL = "external"

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
    running: bool
    ownership: str
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

    def status(self) -> WebappStatus:
        running_here = self._proc is not None and self._proc.poll() is None
        reachable = self.is_reachable() or self.is_port_in_use()

        if running_here and reachable:
            return WebappStatus(
                running=True,
                ownership=OWNERSHIP_OURS,
                pid=self._proc.pid,
                port=self.config.port,
                base_url=self.base_url,
                detail="running (started by this process)",
            )
        if reachable:
            return WebappStatus(
                running=True,
                ownership=OWNERSHIP_EXTERNAL,
                pid=None,
                port=self.config.port,
                base_url=self.base_url,
                detail="running (external — adopted)",
            )
        return WebappStatus(
            running=False,
            ownership=OWNERSHIP_NONE,
            pid=None,
            port=self.config.port,
            base_url=self.base_url,
            detail="not running",
        )

    def start(self, wait: bool = True) -> WebappStatus:
        if not self.config.enabled:
            logger.info("ℹ️  Webapp is disabled in config (webapp.enabled=false)")
            return self.status()

        current = self.status()
        if current.running and current.ownership == OWNERSHIP_OURS:
            logger.info(f"ℹ️  Webapp already {current.detail}")
            return current
        if current.running:
            logger.info(f"🔗 Adopting external webapp at {current.base_url}")
            return current

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
            )
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
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
        if status.running and status.ownership == OWNERSHIP_EXTERNAL:
            raise RuntimeError(
                "Webapp is running but was started externally — cannot restart from here"
            )
        if status.running:
            self.stop()
        return self.start(wait=wait)

    def stop(self) -> WebappStatus:
        status = self.status()
        if status.ownership == OWNERSHIP_EXTERNAL:
            logger.info("✋ Leaving external webapp running (not ours)")
            return status
        if not status.running or self._proc is None:
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

        return WebappStatus(
            running=False,
            ownership=OWNERSHIP_NONE,
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
