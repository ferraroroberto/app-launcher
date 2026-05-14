"""System-tray launcher — owns the webapp + optional Cloudflare tunnel.

Mobile-first design means there's no real desktop UI to surface; the
tray exists so launching `tray.bat` brings the webapp up alongside
Windows login without keeping a console window open.

Menu:
    Open launcher              — open the local URL in the default browser
    Copy local URL             — clipboard the local URL
    Copy Tailscale URL         — clipboard https://<tailscale-host>:8445?token=…
    Copy Cloudflare URL        — clipboard the public URL with ?token=…
    Restart webapp             — stop + start so a new pull is picked up
    Status                     — popup with webapp state
    --
    Quit                       — stop the webapp and exit
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional

import yaml

from src import AppConfig
from src.webapp_config import append_auth_token, load_webapp_config

from app.webapp.manager import (
    WebappManager,
    cert_paths,
    load_config,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TUNNEL_URL_FILE = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"
TUNNEL_CONFIG_PATH = PROJECT_ROOT / "webapp" / "cloudflared.yml"
# The tray runs windowless (pythonw) with no console to log to, so the
# Tailscale lookup leaves a breadcrumb here when it can't resolve a host.
TS_DEBUG_LOG = PROJECT_ROOT / "webapp" / "tailscale_debug.log"


def _read_tunnel_hostname(config_path: Path) -> Optional[str]:
    """Pull the first ingress[].hostname out of the cloudflared config.

    Returns None when the file is missing or unparseable — the tray
    treats either case as "no tunnel" and skips spawning cloudflared.
    """
    if not config_path.exists():
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"⚠️  Could not parse {config_path}: {exc}")
        return None
    for entry in data.get("ingress") or []:
        if isinstance(entry, dict) and entry.get("hostname"):
            return str(entry["hostname"]).strip()
    return None


def _build_icon():
    """Lazy import pystray + Pillow so plain CLI use doesn't drag them in."""
    from PIL import Image

    icon_path = PROJECT_ROOT / "app" / "webapp" / "static" / "icon-512.png"
    if icon_path.exists():
        return Image.open(icon_path)
    return Image.new("RGB", (32, 32), (74, 138, 243))


def _clipboard_copy(text: str) -> bool:
    """Best-effort cross-platform clipboard. Returns True on success."""
    if sys.platform == "win32":
        try:
            p = subprocess.run(
                ["clip"],
                input=text,
                text=True,
                check=False,
                encoding="utf-8",
            )
            return p.returncode == 0
        except OSError as exc:
            logger.debug(f"clip failed: {exc}")
    return False


def _tailscale_binary() -> Optional[str]:
    """Locate the tailscale CLI — PATH first, then the standard Windows install.

    The GUI installer drops ``tailscale.exe`` under ``Program Files`` but
    doesn't always add it to PATH, and the tray is often started by Task
    Scheduler with a minimal environment — so PATH alone isn't enough.
    """
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Tailscale" / "tailscale.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Tailscale" / "tailscale.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _ts_debug(msg: str) -> None:
    """Append a breadcrumb to the Tailscale debug log (best-effort)."""
    logger.debug(f"tailscale: {msg}")
    try:
        TS_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with TS_DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except OSError:
        pass


def _run_tailscale(binary: str, args: list) -> subprocess.CompletedProcess:
    """Run the tailscale CLI windowless, with stdin detached.

    ``CREATE_NO_WINDOW`` stops a console flashing out of the windowless
    tray; ``stdin=DEVNULL`` avoids the invalid-handle trap a ``pythonw``
    parent can hit when a child inherits a missing stdin.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=12,
        check=False,
        creationflags=creationflags,
    )


def _tailscale_hostname() -> Optional[str]:
    """Return this machine's tailnet address, or None if unavailable.

    Prefers the full DNS name (e.g. ``tower.tailnet.ts.net``) — the form
    both the copied URL and the WebAuthn relying-party ID want — and falls
    back to the raw ``100.x`` IP. Every failure path leaves a breadcrumb in
    ``webapp/tailscale_debug.log`` since the tray has no console.
    """
    binary = _tailscale_binary()
    if binary is None:
        _ts_debug("CLI not found on PATH or under Program Files")
        return None
    _ts_debug(f"using binary {binary}")

    # 1. `status --json` → Self.DNSName (the FQDN).
    try:
        result = _run_tailscale(
            binary, ["status", "--self=true", "--peers=false", "--json"]
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _ts_debug(f"status raised {type(exc).__name__}: {exc}")
        result = None
    if result is not None:
        if result.returncode != 0:
            _ts_debug(
                f"status rc={result.returncode} "
                f"stderr={(result.stderr or '').strip()[:200]!r}"
            )
        else:
            try:
                data = json.loads(result.stdout)
                dns = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
                if dns:
                    _ts_debug(f"resolved DNSName {dns}")
                    return dns
                _ts_debug(
                    f"status ok but DNSName empty; "
                    f"BackendState={data.get('BackendState')!r}"
                )
            except ValueError as exc:
                _ts_debug(f"status JSON parse failed: {exc}")

    # 2. Fallback: `tailscale ip -4` → the raw 100.x address.
    try:
        ip_res = _run_tailscale(binary, ["ip", "-4"])
        if ip_res.returncode == 0:
            lines = (ip_res.stdout or "").strip().splitlines()
            ip = lines[0].strip() if lines else ""
            if ip:
                _ts_debug(f"fell back to tailscale ip {ip}")
                return ip
        _ts_debug(
            f"ip -4 rc={ip_res.returncode} "
            f"stderr={(ip_res.stderr or '').strip()[:200]!r}"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _ts_debug(f"ip -4 raised {type(exc).__name__}: {exc}")
    return None


def _notify(title: str, message: str) -> None:
    """Show a Windows toast notification when available; log otherwise."""
    logger.info(f"🔔 {title}: {message}")
    if sys.platform != "win32":
        return
    try:
        from winotify import Notification  # type: ignore

        toast = Notification(app_id="Launcher", title=title, msg=message)
        toast.show()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"winotify failed: {exc}")


def run_tray(app_config: AppConfig) -> int:
    """Run the tray icon. Returns when the user picks Quit."""
    try:
        import pystray  # type: ignore
        from pystray import Menu, MenuItem
    except ImportError as exc:
        logger.error(
            f"❌ pystray not installed ({exc}); install via `pip install -r requirements.txt`"
        )
        return 1

    mgr_cfg = load_config(app_config.webapp)
    manager = WebappManager(mgr_cfg)

    tunnel_hostname = _read_tunnel_hostname(TUNNEL_CONFIG_PATH)
    tunnel_state: dict = {"proc": None}
    session_host_state: dict = {"proc": None}

    starter_error: dict = {"exc": None}

    def _start_session_host():
        """Spawn the loopback PTY session-host and keep a handle on it.

        Owned by the tray exactly like cloudflared — it must outlive webapp
        restarts so running Claude sessions survive a `Restart webapp`.
        """
        cmd = [sys.executable, str(PROJECT_ROOT / "launcher.py"), "session-host"]
        kw: dict = dict(
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kw["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        try:
            proc = subprocess.Popen(cmd, **kw)
        except OSError as exc:
            logger.warning(f"⚠️  session-host failed to launch: {exc}")
            _notify("Session host", f"Failed to start: {exc}")
            return
        session_host_state["proc"] = proc
        logger.info(f"🧩 session-host started (pid={proc.pid})")

    def _stop_session_host():
        proc = session_host_state.get("proc")
        session_host_state["proc"] = None
        if proc is None:
            return
        try:
            logger.info(f"🛑 Stopping session-host (pid={proc.pid})")
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
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"session-host stop failed: {exc}")

    threading.Thread(target=_start_session_host, daemon=True).start()

    def _start():
        try:
            manager.start(wait=True)
            _notify("Launcher webapp ready", manager.base_url)
        except Exception as exc:  # noqa: BLE001
            starter_error["exc"] = exc
            logger.error(f"❌ webapp start failed: {exc}")
            _notify("Launcher start failed", str(exc))

    threading.Thread(target=_start, daemon=True).start()

    def _start_tunnel():
        if tunnel_hostname is None:
            return
        bin_path = shutil.which("cloudflared")
        if bin_path is None:
            logger.warning(
                "⚠️  cloudflared not on PATH — public URL won't be reachable. "
                "Install: winget install Cloudflare.cloudflared"
            )
            _notify(
                "Cloudflare tunnel",
                "cloudflared not on PATH — install via winget",
            )
            return
        cmd = [
            bin_path, "tunnel", "--config", str(TUNNEL_CONFIG_PATH), "run",
        ]
        kw: dict = dict(
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kw["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        try:
            proc = subprocess.Popen(cmd, **kw)
        except OSError as exc:
            logger.warning(f"⚠️  cloudflared failed to launch: {exc}")
            _notify("Cloudflare tunnel", f"Failed to start: {exc}")
            return
        tunnel_state["proc"] = proc
        logger.info(
            f"🌍 Cloudflare tunnel started → https://{tunnel_hostname} "
            f"(pid={proc.pid})"
        )

        url = f"https://{tunnel_hostname}"
        token = (load_webapp_config().auth_token or "").strip()
        if token:
            url = append_auth_token(url, token)
        try:
            TUNNEL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
            TUNNEL_URL_FILE.write_text(url + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning(f"⚠️  Could not write {TUNNEL_URL_FILE}: {exc}")

    def _stop_tunnel():
        proc = tunnel_state.get("proc")
        tunnel_state["proc"] = None
        if proc is None:
            return
        try:
            logger.info(f"🛑 Stopping cloudflared (pid={proc.pid})")
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
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"cloudflared stop failed: {exc}")
        try:
            if TUNNEL_URL_FILE.exists():
                TUNNEL_URL_FILE.unlink()
        except OSError:
            pass

    if tunnel_hostname is not None:
        threading.Thread(target=_start_tunnel, daemon=True).start()

    def open_local(icon, item):  # noqa: ARG001
        webbrowser.open(manager.base_url)

    def copy_local(icon, item):  # noqa: ARG001
        webapp_cfg = load_webapp_config()
        url = append_auth_token(manager.base_url, webapp_cfg.auth_token)
        if _clipboard_copy(url):
            _notify("Copied local URL", url)
        else:
            _notify("Local URL", url)

    def copy_tailscale(icon, item):  # noqa: ARG001
        host = _tailscale_hostname()
        if not host:
            reason = ""
            try:
                lines = TS_DEBUG_LOG.read_text(
                    encoding="utf-8"
                ).strip().splitlines()
                reason = lines[-1] if lines else ""
            except OSError:
                pass
            _notify(
                "Tailscale not available",
                reason
                or "Couldn't resolve a tailnet address — see webapp/tailscale_debug.log.",
            )
            return
        scheme = "https" if cert_paths() else "http"
        url = f"{scheme}://{host}:{manager.config.port}"
        webapp_cfg = load_webapp_config()
        url = append_auth_token(url, webapp_cfg.auth_token)
        if _clipboard_copy(url):
            _notify("Copied Tailscale URL", url)
        else:
            _notify("Tailscale URL", url)

    def copy_tunnel(icon, item):  # noqa: ARG001
        if not TUNNEL_URL_FILE.exists():
            _notify(
                "No tunnel URL yet",
                "Run webapp_tunnel_named.bat to bring up the Cloudflare tunnel.",
            )
            return
        try:
            url = TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _notify("Tunnel URL read failed", str(exc))
            return
        if not url:
            _notify("Tunnel URL is empty", str(TUNNEL_URL_FILE))
            return
        if _clipboard_copy(url):
            _notify("Copied Cloudflare URL", url)
        else:
            _notify("Cloudflare URL", url)

    def restart_webapp(icon, item):  # noqa: ARG001
        def _do_restart():
            try:
                _notify("Launcher", "Restarting webapp…")
                manager.restart(wait=True)
                _notify("Launcher webapp restarted", manager.base_url)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"❌ webapp restart failed: {exc}")
                _notify("Restart failed", str(exc))

        threading.Thread(target=_do_restart, daemon=True).start()

    def enroll_device(icon, item):  # noqa: ARG001
        """Open a one-time passkey enrollment window on the webapp.

        Opening it deliberately from the PC is what makes adding a new
        device to the terminal whitelist a conscious act.
        """
        def _do_enroll():
            scheme = "https" if cert_paths() else "http"
            url = (
                f"{scheme}://127.0.0.1:{manager.config.port}"
                "/api/webauthn/enroll/window"
            )
            try:
                import requests

                resp = requests.post(
                    url, json={"seconds": 300}, timeout=5, verify=False
                )
                if resp.status_code == 200:
                    _notify(
                        "Passkey enrollment",
                        "5-minute window open — register your iPhone now "
                        "from the launcher's terminal screen.",
                    )
                else:
                    _notify(
                        "Passkey enrollment failed",
                        f"HTTP {resp.status_code}: {resp.text[:120]}",
                    )
            except Exception as exc:  # noqa: BLE001
                _notify("Passkey enrollment failed", str(exc))

        threading.Thread(target=_do_enroll, daemon=True).start()

    def show_status(icon, item):  # noqa: ARG001
        s = manager.status()
        _notify("Launcher status", f"{s.detail} · {s.base_url}")

    def quit_app(icon, item):  # noqa: ARG001
        logger.info("👋 Tray quit requested")
        _stop_tunnel()
        _stop_session_host()
        try:
            manager.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️  stop failed: {exc}")
        icon.stop()

    def on_left_click(icon, item):  # noqa: ARG001
        webbrowser.open(manager.base_url)

    menu = Menu(
        MenuItem("🚀 Open launcher", on_left_click, default=True),
        MenuItem("📋 Copy local URL", copy_local),
        MenuItem("📋 Copy Tailscale URL", copy_tailscale),
        MenuItem("📋 Copy Cloudflare URL", copy_tunnel),
        Menu.SEPARATOR,
        MenuItem("🔄 Restart webapp", restart_webapp),
        MenuItem("🔐 Enroll device (5 min)", enroll_device),
        MenuItem("ℹ️ Status", show_status),
        Menu.SEPARATOR,
        MenuItem("🚪 Quit", quit_app),
    )

    icon = pystray.Icon(
        "launcher",
        icon=_build_icon(),
        title="Launcher",
        menu=menu,
    )
    icon.run()
    if starter_error["exc"] is not None:
        return 1
    return 0
