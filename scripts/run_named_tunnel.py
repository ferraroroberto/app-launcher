"""Start uvicorn + cloudflared on a named (persistent) tunnel.

Used by ``webapp_tunnel_named.bat`` for headless / no-tray use. The
tray already does this same work as part of normal startup — only
reach for this script when running without the tray.

Boots:

  1. uvicorn (HTTPS if ``webapp/certificates/cert.pem`` exists)
  2. cloudflared tunnel --config webapp/cloudflared.yml run

The persistent URL is written to ``webapp/last_tunnel_url.txt`` (with
``?token=...`` appended when an ``auth_token`` is configured).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional


logger = logging.getLogger("run_named_tunnel")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src._tunnel_config import read_tunnel_hostname  # noqa: E402
from src.subprocess_flags import NO_WINDOW_NEW_GROUP  # noqa: E402
from app.webapp.manager import (  # noqa: E402
    WebappManager,
    WebappManagerConfig,
    check_tailscale_cert,
)

DEFAULT_CONFIG = PROJECT_ROOT / "webapp" / "cloudflared.yml"
SAMPLE_CONFIG = PROJECT_ROOT / "webapp" / "cloudflared.sample.yml"
TUNNEL_URL_FILE = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"
DEFAULT_PORT = 8445


def _have_listener(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _spawn_uvicorn(port: int) -> subprocess.Popen:
    """Start uvicorn with the *same* argv the app builds everywhere else.

    This script used to compose its own uvicorn command line (#1003), and
    it had already drifted: the ``--loop`` selector shim every other spawn
    site passes went missing here, so the documented no-tray path booted on
    the Windows proactor loop whose accept path closes the listening socket
    on one aborted client (#388, fixed in #1007). Building the argv through
    :meth:`WebappManager._build_command` means the next uvicorn flag lands
    in both places at once instead of only in the app.

    The spawn itself stays here: this script owns the process, streams its
    output and forwards signals, so it keeps its own ``Popen`` rather than
    handing lifecycle to the manager.
    """
    # Same Tailscale cert auto-renew the app runs before binding
    # (project-scaffolding#89) — shared rather than re-spawned by hand.
    check_tailscale_cert()
    cmd = WebappManager(
        WebappManagerConfig(host="127.0.0.1", port=port)
    )._build_command()
    logger.info(f"🚀 Starting uvicorn: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        creationflags=NO_WINDOW_NEW_GROUP,
    )


def _wait_for_uvicorn(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _have_listener(port):
            return True
        time.sleep(0.3)
    return False


def _spawn_cloudflared(config_path: Path) -> subprocess.Popen:
    bin_path = shutil.which("cloudflared")
    if bin_path is None:
        raise SystemExit(
            "❌ cloudflared not found on PATH. Install: "
            "winget install Cloudflare.cloudflared"
        )
    cmd = [bin_path, "tunnel", "--config", str(config_path), "run"]
    logger.info(f"🌐 Starting cloudflared: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=NO_WINDOW_NEW_GROUP,
    )


def _read_auth_token() -> str:
    try:
        from src.webapp_config import load_webapp_config

        return (load_webapp_config().auth_token or "").strip()
    except Exception as exc:
        logger.debug(f"could not read auth_token: {exc}")
        return ""


def _persist_tunnel_url(hostname: str) -> None:
    url = f"https://{hostname}"
    token = _read_auth_token()
    if token:
        from src.webapp_config import append_auth_token

        url = append_auth_token(url, token)
    try:
        TUNNEL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        TUNNEL_URL_FILE.write_text(url + "\n", encoding="utf-8")
        logger.info(f"📡 Tunnel URL → {TUNNEL_URL_FILE}")
        logger.info(f"   {url}")
        if token:
            logger.info(
                "🔐 auth_token is set — the URL above includes ?token=… so "
                "the phone bootstraps on first load."
            )
    except OSError as exc:
        logger.warning(f"⚠️  Could not write {TUNNEL_URL_FILE}: {exc}")


def _stream(proc: subprocess.Popen) -> None:
    for line in proc.stdout or ():
        sys.stdout.write(line)
        sys.stdout.flush()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(os.environ.get("CLOUDFLARED_CONFIG", str(DEFAULT_CONFIG)))
    if not config_path.exists():
        logger.error(
            f"❌ {config_path} missing. Copy {SAMPLE_CONFIG.name} to "
            f"{config_path.name} and fill in your tunnel UUID + hostname."
        )
        return 1

    hostname = read_tunnel_hostname(config_path)
    if hostname:
        logger.info(f"🌍 Public hostname: https://{hostname}")
    else:
        logger.warning(
            "⚠️  No hostname found in ingress[] — tunnel will still run "
            "but last_tunnel_url.txt won't be updated."
        )

    port = int(os.environ.get("WEBAPP_PORT", DEFAULT_PORT))
    uvicorn_proc: Optional[subprocess.Popen] = None
    if _have_listener(port):
        logger.info(f"🔗 Adopting existing webapp on :{port}")
    else:
        uvicorn_proc = _spawn_uvicorn(port)
        if not _wait_for_uvicorn(port):
            logger.error("❌ uvicorn failed to start within 15 s")
            if uvicorn_proc is not None:
                uvicorn_proc.terminate()
            return 1

    cloudflared = _spawn_cloudflared(config_path)
    streamer = threading.Thread(target=_stream, args=(cloudflared,), daemon=True)
    streamer.start()

    if hostname:
        _persist_tunnel_url(hostname)

    try:
        cloudflared.wait()
    except KeyboardInterrupt:
        logger.info("⏹️  Ctrl+C — shutting down")
    finally:
        for proc, name in ((cloudflared, "cloudflared"), (uvicorn_proc, "uvicorn")):
            if proc is None:
                continue
            try:
                logger.info(f"🛑 Stopping {name} (pid={proc.pid})")
                if sys.platform == "win32":
                    try:
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                    except Exception:
                        pass
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:
                logger.debug(f"{name} stop failed: {exc}")
        try:
            if TUNNEL_URL_FILE.exists():
                TUNNEL_URL_FILE.unlink()
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
