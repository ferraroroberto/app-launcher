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
import socket
import subprocess
import sys
import threading
import time
import tomllib
import webbrowser
from pathlib import Path
from typing import Optional

import yaml

from src import AppConfig
from src.registry import load_registry
from src.scanner import KIND_TRAY
from src.webapp_config import append_auth_token, load_webapp_config

from app.tray.single_instance import SingleInstance
from app.tray.watchdog import HealthWatchdog
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
# Same reason: the health watchdog (issue #386) leaves its wedge/recovery
# breadcrumbs here — the only durable trail of *when* :8445 stopped answering.
WATCHDOG_LOG = PROJECT_ROOT / "webapp" / "watchdog.log"

# The loopback PTY session-host. It is a *linked-but-independent* child
# (project-scaffolding#35): it hosts the user's Coding PTYs and MUST survive a
# `tray.bat --restart`. So it is spawned detached (re-parented out of the tray
# subtree) and adopted on start by this port, and :8446 is excluded from
# tray.bat's reclaim sweep.
SESSION_HOST_PORT = 8446

# Registered Trays sequential boot (issue #456 part 2/2).
_TRAY_READY_TIMEOUT_S = 30.0
_TRAY_READY_POLL_S = 0.5
# Fallback wait when a tray's repo has no readable .fleet.toml port — still
# gives it a head start before the next tray launches, without a real signal.
_TRAY_FALLBACK_DELAY_S = 5.0


def _port_listening(port: int) -> bool:
    """True if something is listening on 127.0.0.1:<port> (loopback)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _fleet_toml_port(repo_dir: Path) -> Optional[int]:
    """Read the ``port`` field out of ``repo_dir/.fleet.toml``.

    This is the fleet-wide anti-staleness-enforced convention every
    tray-owning repo already keeps current (project-scaffolding#83/#148) —
    the only per-repo-agnostic readiness signal available here. PID-tree
    port discovery (as the Running Apps panel uses for launcher-spawned
    bats) does NOT work for a Registered Trays entry: every sister
    ``tray.bat`` hands off via ``tray_lifecycle.ps1``'s ``Start-Process``
    (verified in that shared script), which detaches the real long-lived
    tray process from the invoking ``tray.bat``'s own process — that
    invoking process exits within about a second, well before any webapp
    binds its port, so a PID-tree walk rooted there finds nothing.

    Accepts both shapes seen across the fleet: a bare int (``port =
    8447``) or a leading-colon string (``port = ":8445"``). Returns
    ``None`` on a missing file, missing field, or an unrecognised shape —
    the caller falls back to a fixed delay.
    """
    toml_path = repo_dir / ".fleet.toml"
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    raw = data.get("port")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.lstrip(":"))
        except ValueError:
            return None
    return None


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

    tray_ico = PROJECT_ROOT / "assets" / "tray" / "app-launcher.ico"
    if tray_ico.exists():
        return Image.open(tray_ico)
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


def _breadcrumb(path: Path, msg: str) -> None:
    """Append a timestamped breadcrumb line to ``path`` (best-effort)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except OSError:
        pass


def _ts_debug(msg: str) -> None:
    """Append a breadcrumb to the Tailscale debug log (best-effort)."""
    logger.debug(f"tailscale: {msg}")
    _breadcrumb(TS_DEBUG_LOG, msg)


def _wd_log(msg: str) -> None:
    """Append a breadcrumb to the watchdog log (best-effort)."""
    logger.debug(f"watchdog: {msg}")
    _breadcrumb(WATCHDOG_LOG, msg)


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


class TrayApp:
    """Owns the webapp + tunnel + session-host lifecycle behind the tray icon.

    Promoted from a pile of closures sharing mutable state via single-key
    dict boxes (a workaround for closures' lack of `nonlocal` rebinding) to
    a class with real instance attributes and bound methods — same
    behavior, independently readable/testable state.
    """

    def __init__(self, app_config: AppConfig, instance: SingleInstance) -> None:
        # `instance` is intentionally kept referenced for the tray's
        # lifetime (released in quit_app); the OS frees the named mutex on
        # process exit either way, but an early GC would free it sooner.
        self.app_config = app_config
        self.instance = instance
        self.manager = WebappManager(load_config(app_config.webapp))
        self.tunnel_hostname = _read_tunnel_hostname(TUNNEL_CONFIG_PATH)
        self.tunnel_proc: Optional[subprocess.Popen] = None
        self.starter_exc: Optional[Exception] = None

        # Health watchdog (issue #386): a wedged uvicorn still LISTENs, so
        # only a real /healthz round-trip can tell "up" from "hung".
        # Alerting only — recovery stays manual (tray.bat --restart) until
        # the failure mode is understood; no improvised process kills.
        self.watchdog_stop = threading.Event()
        self.watchdog = HealthWatchdog(
            probe=self.manager.is_reachable,
            on_wedge=self._on_webapp_wedge,
            on_recover=self._on_webapp_recover,
        )

    # -- session-host lifecycle -------------------------------------------

    def _start_session_host(self) -> None:
        """Bring up the loopback PTY session-host, ADOPTING one that already
        survived a tray restart instead of spawning a duplicate.

        Linked-but-independent (project-scaffolding#35): it hosts the user's
        Coding PTYs, which MUST survive `tray.bat --restart`. So it is spawned
        DETACHED via `cmd /c start` — re-parented out of this tray's process
        subtree so `taskkill /T` cannot reach it. (DETACHED_PROCESS /
        CREATE_NEW_PROCESS_GROUP do NOT escape /T — it walks the parent-child PID
        tree; only re-parenting does. Verified empirically.) :8446 is also
        excluded from tray.bat's reclaim sweep. We keep no Popen handle — the
        session-host is managed by port identity (adopt on start, reclaim on
        Quit), not by parentage.
        """
        if _port_listening(SESSION_HOST_PORT):
            logger.info(f"🔗 Adopting running session-host on :{SESSION_HOST_PORT}")
            return
        # `start` launches the child and cmd exits, orphaning it out of this
        # tray's subtree; /b keeps it windowless (the pythonw child is windowless
        # anyway). CREATE_NO_WINDOW hides the transient cmd.
        cmd = [
            "cmd", "/c", "start", "", "/b",
            sys.executable, str(PROJECT_ROOT / "launcher.py"), "session-host",
        ]
        kw: dict = dict(
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            subprocess.Popen(cmd, **kw)
        except OSError as exc:
            logger.warning(f"⚠️  session-host failed to launch: {exc}")
            _notify("Session host", f"Failed to start: {exc}")
            return
        logger.info(f"🧩 session-host spawned detached on :{SESSION_HOST_PORT}")

    def _stop_session_host(self) -> None:
        """Stop the session-host on an explicit Quit. It is detached (no Popen
        handle), so reclaim it by its owned port, scoped to this repo's .venv so
        a sibling app's process is never touched. Reuses the canonical
        tray_lifecycle.ps1 `reclaim` action (same one tray.bat --restart uses
        for the webapp port) instead of hand-rolling the venv-scoped kill.

        tray_lifecycle.ps1 is the ONE shared, machine-local copy owned by
        fleet-config (project-scaffolding#153) — junctioned by fleet-config's
        install.ps1 into %USERPROFILE%\\.claude\\tray, never vendored per-app.
        Same resolved path as tray.bat's TRAY_PS (#433: this used to point at
        a nonexistent repo-local path and silently no-op).
        """
        if not _port_listening(SESSION_HOST_PORT):
            return
        tray_ps = Path(os.environ["USERPROFILE"]) / ".claude" / "tray" / "tray_lifecycle.ps1"
        if not tray_ps.exists():
            msg = f"missing shared tray helper {tray_ps} — session-host on :{SESSION_HOST_PORT} left running"
            logger.warning(f"⚠️  {msg}")
            _notify("Session host stop failed", msg)
            return
        try:
            logger.info(f"🛑 Stopping session-host on :{SESSION_HOST_PORT}")
            result = subprocess.run(
                [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                 "-NoProfile", "-NonInteractive", "-File",
                 str(tray_ps),
                 "reclaim",
                 "-VenvDir", str(PROJECT_ROOT / ".venv"),
                 "-Ports", str(SESSION_HOST_PORT)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                msg = f"tray_lifecycle.ps1 reclaim exited {result.returncode} — session-host on :{SESSION_HOST_PORT} may still be running"
                logger.warning(f"⚠️  {msg}")
                _notify("Session host stop failed", msg)
        except Exception as exc:  # noqa: BLE001
            msg = f"session-host stop failed: {exc}"
            logger.warning(f"⚠️  {msg}")
            _notify("Session host stop failed", msg)

    # -- Registered Trays sequential boot (issue #456 part 2/2) --------------

    def _spawn_tray_bat_detached(self, bat_path: Path) -> None:
        """Quiet-launch a Registered Trays entry's ``tray.bat``.

        Detached the same way as :meth:`_start_session_host` — re-parented
        out of this tray's own process subtree via ``cmd /c start`` — so a
        later ``tray.bat --restart`` on THIS machine never touches it (it
        isn't this repo's process to manage beyond starting it).
        """
        cmd = ["cmd", "/c", "start", "", "/b", str(bat_path)]
        kw: dict = dict(
            cwd=str(bat_path.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(cmd, **kw)

    def _wait_for_tray_ready(self, repo_dir: Path) -> bool:
        """Best-effort readiness wait for a just-launched sister tray.

        See :func:`_fleet_toml_port` for why this is `.fleet.toml`-port
        based rather than PID-tree port discovery. No/malformed port
        declaration → a fixed delay stand-in, returning ``False`` (not
        confirmed ready, but gave it a head start).
        """
        port = _fleet_toml_port(repo_dir)
        if port is None:
            time.sleep(_TRAY_FALLBACK_DELAY_S)
            return False
        deadline = time.monotonic() + _TRAY_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if _port_listening(port):
                return True
            time.sleep(_TRAY_READY_POLL_S)
        return False

    def _launch_registered_trays(self) -> None:
        """Walk autostart-enabled Registered Trays entries one at a time,
        waiting for each to report ready before starting the next — avoids
        a boot-time CPU/disk spike from launching several sister
        Python/Streamlit processes concurrently. The registry's existing
        alphabetical order (see :func:`src.registry.persist_additions`) is
        the "fixed order" the issue accepts for v1 — no UI reordering.

        One entry failing to launch, or its readiness check timing out,
        is logged and does NOT abort the rest of the sequence — this
        method itself must never raise into :meth:`_start`'s caller.
        """
        try:
            registry = load_registry()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️  Registered Trays: could not load registry: {exc}")
            return
        trays = [a for a in registry.apps if a.kind == KIND_TRAY and a.autostart]
        if not trays:
            return
        logger.info(f"🧭 Registered Trays: launching {len(trays)} autostart entr{'y' if len(trays) == 1 else 'ies'}")
        for entry in trays:
            if not entry.bat_path:
                logger.warning(f"⚠️  Registered Trays: {entry.name} has no bat_path — skipped")
                continue
            bat_path = Path(entry.bat_path)
            if not bat_path.is_file():
                logger.warning(f"⚠️  Registered Trays: {entry.name} bat not found at {bat_path} — skipped")
                continue
            try:
                logger.info(f"🚀 Registered Trays: launching {entry.name}")
                self._spawn_tray_bat_detached(bat_path)
                ready = self._wait_for_tray_ready(bat_path.parent)
                if ready:
                    logger.info(f"✅ Registered Trays: {entry.name} ready")
                else:
                    logger.warning(
                        f"⚠️  Registered Trays: {entry.name} readiness unconfirmed "
                        "(timed out or no .fleet.toml port) — continuing"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"⚠️  Registered Trays: {entry.name} failed to launch: {exc}")
                continue

    # -- webapp lifecycle ----------------------------------------------------

    def _start(self) -> None:
        try:
            self.manager.start(wait=True)
            _notify("Launcher webapp ready", self.manager.base_url)
            self._launch_registered_trays()
        except Exception as exc:  # noqa: BLE001
            self.starter_exc = exc
            logger.error(f"❌ webapp start failed: {exc}")
            _notify("Launcher start failed", str(exc))

    def _on_webapp_wedge(self, failures: int) -> None:
        msg = (
            f"webapp on :{self.manager.config.port} stopped answering /healthz "
            f"({failures} consecutive probes) — restart with tray.bat --restart"
        )
        logger.error(f"❌ {msg}")
        _wd_log(f"WEDGE {msg}")
        _notify("Launcher webapp unresponsive", msg)

    def _on_webapp_recover(self) -> None:
        msg = f"webapp on :{self.manager.config.port} answering /healthz again"
        logger.info(f"✅ {msg}")
        _wd_log(f"RECOVERED {msg}")
        _notify("Launcher webapp recovered", msg)

    # -- Cloudflare tunnel lifecycle ------------------------------------------

    def _start_tunnel(self) -> None:
        if self.tunnel_hostname is None:
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
        self.tunnel_proc = proc
        logger.info(
            f"🌍 Cloudflare tunnel started → https://{self.tunnel_hostname} "
            f"(pid={proc.pid})"
        )

        url = f"https://{self.tunnel_hostname}"
        token = (load_webapp_config().auth_token or "").strip()
        if token:
            url = append_auth_token(url, token)
        try:
            TUNNEL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
            TUNNEL_URL_FILE.write_text(url + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning(f"⚠️  Could not write {TUNNEL_URL_FILE}: {exc}")

    def _stop_tunnel(self) -> None:
        proc = self.tunnel_proc
        self.tunnel_proc = None
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

    # -- menu actions ----------------------------------------------------

    def open_local(self, icon, item) -> None:  # noqa: ARG002
        webbrowser.open(self.manager.base_url)

    def copy_local(self, icon, item) -> None:  # noqa: ARG002
        webapp_cfg = load_webapp_config()
        url = append_auth_token(self.manager.base_url, webapp_cfg.auth_token)
        if _clipboard_copy(url):
            _notify("Copied local URL", url)
        else:
            _notify("Local URL", url)

    def copy_tailscale(self, icon, item) -> None:  # noqa: ARG002
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
        url = f"{scheme}://{host}:{self.manager.config.port}"
        webapp_cfg = load_webapp_config()
        url = append_auth_token(url, webapp_cfg.auth_token)
        if _clipboard_copy(url):
            _notify("Copied Tailscale URL", url)
        else:
            _notify("Tailscale URL", url)

    def copy_tunnel(self, icon, item) -> None:  # noqa: ARG002
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

    def restart_webapp(self, icon, item) -> None:  # noqa: ARG002
        def _do_restart():
            try:
                _notify("Launcher", "Restarting webapp…")
                self.manager.restart(wait=True)
                _notify("Launcher webapp restarted", self.manager.base_url)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"❌ webapp restart failed: {exc}")
                _notify("Restart failed", str(exc))

        threading.Thread(target=_do_restart, daemon=True).start()

    def enroll_device(self, icon, item) -> None:  # noqa: ARG002
        """Open a one-time passkey enrollment window on the webapp.

        Opening it deliberately from the PC is what makes adding a new
        device to the terminal whitelist a conscious act.
        """
        def _do_enroll():
            scheme = "https" if cert_paths() else "http"
            url = (
                f"{scheme}://127.0.0.1:{self.manager.config.port}"
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

    def show_status(self, icon, item) -> None:  # noqa: ARG002
        s = self.manager.status()
        _notify("Launcher status", f"{s.detail} · {s.base_url}")

    def quit_app(self, icon, item) -> None:  # noqa: ARG002
        logger.info("👋 Tray quit requested")
        self.watchdog_stop.set()
        self._stop_tunnel()
        self._stop_session_host()
        try:
            self.manager.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️  stop failed: {exc}")
        self.instance.release()
        icon.stop()

    def on_left_click(self, icon, item) -> None:  # noqa: ARG002
        webbrowser.open(self.manager.base_url)

    # -- run ---------------------------------------------------------------

    def run(self) -> int:
        """Start background threads, build the menu + icon, block until Quit."""
        import pystray  # type: ignore
        from pystray import Menu, MenuItem

        threading.Thread(target=self._start_session_host, daemon=True).start()
        threading.Thread(target=self._start, daemon=True).start()
        threading.Thread(
            target=self.watchdog.run, args=(self.watchdog_stop,), daemon=True
        ).start()
        if self.tunnel_hostname is not None:
            threading.Thread(target=self._start_tunnel, daemon=True).start()

        menu = Menu(
            MenuItem("🚀 Open launcher", self.on_left_click, default=True),
            MenuItem("📋 Copy local URL", self.copy_local),
            MenuItem("📋 Copy Tailscale URL", self.copy_tailscale),
            MenuItem("📋 Copy Cloudflare URL", self.copy_tunnel),
            Menu.SEPARATOR,
            MenuItem("🔄 Restart webapp", self.restart_webapp),
            MenuItem("🔐 Enroll device (5 min)", self.enroll_device),
            MenuItem("ℹ️ Status", self.show_status),
            Menu.SEPARATOR,
            MenuItem("🚪 Quit", self.quit_app),
        )

        icon = pystray.Icon(
            "launcher",
            icon=_build_icon(),
            title="Launcher",
            menu=menu,
        )
        icon.run()
        if self.starter_exc is not None:
            return 1
        return 0


def run_tray(app_config: AppConfig) -> int:
    """Run the tray icon. Returns when the user picks Quit."""
    try:
        import pystray  # noqa: F401  (import-check only; TrayApp.run() re-imports)
    except ImportError as exc:
        logger.error(
            f"❌ pystray not installed ({exc}); install via `pip install -r requirements.txt`"
        )
        return 1

    # In-process single-instance guard (project-scaffolding#39): the tray.bat CIM
    # pre-check can let two near-simultaneous launches through, so the guarantee
    # must live in the process. Held for the tray's lifetime; the OS frees the
    # named mutex on exit.
    instance = SingleInstance(r"Global\app-launcher-tray")
    if not instance.acquired:
        logger.info("ℹ️  Another app-launcher tray is already running; exiting.")
        return 0

    return TrayApp(app_config, instance).run()
