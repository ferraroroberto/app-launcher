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

Split off a single-file god-module (``/codebase-audit``): Tailscale CLI
discovery + hostname resolution live in :mod:`app.tray.tailscale`, and the
Registered Trays sequential autostart boot sequence lives in
:mod:`app.tray.registered_trays` — both pure functions with no ``TrayApp``
state, imported here rather than owning their own copy of the logic.
"""

from __future__ import annotations

import datetime
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


from src import AppConfig
from src._tunnel_config import read_tunnel_hostname
from src.subprocess_flags import NO_WINDOW, NO_WINDOW_NEW_GROUP
from src.webapp_config import append_auth_token, load_webapp_config

from app.tray import registered_trays, tailscale
from app.tray.self_heal import RestartBudget, SelfHealer, should_heal_webapp
from app.tray.single_instance import SingleInstance
from app.tray.watchdog import DEFAULT_FAILURES_TO_ALERT, HealthWatchdog
from app.webapp.manager import (
    HEALTH_ANSWERING,
    HEALTH_BOUND_NOT_ANSWERING,
    OWNERSHIP_OURS,
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
# Same reason: the health watchdog leaves its wedge/recovery
# breadcrumbs here — the only durable trail of *when* :8445 stopped answering.
WATCHDOG_LOG = PROJECT_ROOT / "webapp" / "watchdog.log"
# Where the detached session-host's own stdout/stderr lands. It is
# spawned re-parented out of this tray's subtree and has no console, so this
# file is the only place its logging can go.
SESSION_HOST_LOG = PROJECT_ROOT / "webapp" / "session-host.log"
# Two missed 60 s probes before the session-host counts as gone: a tray
# restart or an operator's deliberate reclaim-then-start is over well inside
# that, so the watchdog never races a human respawning it.
SESSION_HOST_FAILURES_TO_ALERT = 2
# Bound on one tray_lifecycle.ps1 reclaim (a PowerShell cold start, a CIM
# query and a Stop-Process): an admitted guess with room over the few
# seconds it normally takes, so a hung PowerShell cannot stall the caller.
RECLAIM_TIMEOUT_S = 10

# The loopback PTY session-host. It is a *linked-but-independent* child:
# it hosts the user's Coding PTYs and MUST survive a
# `tray.bat --restart`. So it is spawned detached (re-parented out of the tray
# subtree) and adopted on start by its own port, and that port is excluded
# from tray.bat's reclaim sweep. 8446 is only the *default* — the real,
# user-editable value is ``load_webapp_config().session_host_port``
# (README.md "session_host_port"). Never hardcode the port value as a
# separate definition here alongside ``src/webapp_config.py``'s
# ``DEFAULT_SESSION_HOST_PORT`` and ``app/session_host/server.py``'s
# ``DEFAULT_PORT`` — a second definition can drift from the configured
# value, leaving the tray adopting/reclaiming the wrong port and silently
# spawning a duplicate session-host on every start. Always call
# :func:`_session_host_port` fresh rather than caching a module constant.


def _session_host_port() -> int:
    """The configured session-host port — read fresh, never cached, so a
    change to the ``session_host_port`` setting takes effect on the tray's
    very next start/stop of the process."""
    return load_webapp_config().session_host_port


def _reclaim_port(port: int) -> Optional[str]:
    """Kill whatever process of THIS repo's .venv listens on ``port``.
    Returns ``None`` on success, else why it could not.

    Reuses the canonical tray_lifecycle.ps1 `reclaim` action (the same
    one tray.bat --restart uses for the webapp port) instead of
    hand-rolling the venv-scoped kill, so a sibling app's process is never
    touched. tray_lifecycle.ps1 is the ONE shared, machine-local copy
    owned by fleet-config, junctioned by fleet-config's install.ps1 into
    %USERPROFILE%\\.claude\\tray and never vendored per-app, at the
    same resolved path as tray.bat's TRAY_PS.
    """
    tray_ps = Path(os.environ["USERPROFILE"]) / ".claude" / "tray" / "tray_lifecycle.ps1"
    if not tray_ps.exists():
        return f"missing shared tray helper {tray_ps}"
    try:
        result = subprocess.run(
            [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
             "-NoProfile", "-NonInteractive", "-File",
             str(tray_ps),
             "reclaim",
             "-VenvDir", str(PROJECT_ROOT / ".venv"),
             "-Ports", str(port)],
            creationflags=NO_WINDOW,
            timeout=RECLAIM_TIMEOUT_S,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"reclaim of :{port} failed: {exc}"
    if result.returncode != 0:
        return f"tray_lifecycle.ps1 reclaim exited {result.returncode}"
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
                creationflags=NO_WINDOW,
            )
            return p.returncode == 0
        except OSError as exc:
            logger.debug(f"clip failed: {exc}")
    return False


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
        self.tunnel_hostname = read_tunnel_hostname(TUNNEL_CONFIG_PATH)
        self.tunnel_proc: Optional[subprocess.Popen] = None
        self.starter_exc: Optional[Exception] = None

        # Health watchdog: a wedged uvicorn still LISTENs, so
        # only a real /healthz round-trip can tell "up" from "hung". A
        # webapp that stays failing is restarted by the bounded self-heal;
        # a one-shot start that found the port held would otherwise leave it
        # dead until someone ran tray.bat --restart by hand.
        self.watchdog_stop = threading.Event()
        self.webapp_healer = SelfHealer(
            "webapp", self._restart_webapp_for_heal, RestartBudget(), _wd_log
        )
        self.watchdog = HealthWatchdog(
            probe=self.manager.is_reachable,
            on_wedge=self._on_webapp_wedge,
            on_recover=self._on_webapp_recover,
            on_failing=self._heal_webapp,
        )
        # The session-host is watched for "nothing listening" only. A
        # listening session-host may be holding live PTYs however slowly it
        # answers, so this never kills one: it only respawns a dead one.
        self.session_host_healer = SelfHealer(
            "session-host", self._start_session_host, RestartBudget(), _wd_log
        )
        self.session_host_watchdog = HealthWatchdog(
            probe=lambda: registered_trays.port_listening(_session_host_port()),
            on_wedge=self._on_session_host_down,
            on_recover=self._on_session_host_recover,
            failures_to_alert=SESSION_HOST_FAILURES_TO_ALERT,
            on_failing=self._heal_session_host,
        )

    # -- session-host lifecycle -------------------------------------------

    def _start_session_host(self) -> str:
        """Bring up the loopback PTY session-host, ADOPTING one that already
        survived a tray restart instead of spawning a duplicate.

        Linked-but-independent (project-scaffolding#35): it hosts the user's
        Coding PTYs, which MUST survive `tray.bat --restart`. So it is spawned
        DETACHED via `cmd /c start` — re-parented out of this tray's process
        subtree so `taskkill /T` cannot reach it. (DETACHED_PROCESS /
        CREATE_NEW_PROCESS_GROUP do NOT escape /T — it walks the parent-child PID
        tree; only re-parenting does. Verified empirically.) Its port is also
        excluded from tray.bat's reclaim sweep. We keep no Popen handle — the
        session-host is managed by port identity (adopt on start, reclaim on
        Quit), not by parentage.

        Returns a short outcome line for the watchdog log.
        """
        port = _session_host_port()
        if registered_trays.port_listening(port):
            logger.info(f"🔗 Adopting running session-host on :{port}")
            return f"adopted the session-host already listening on :{port}"
        # `start` launches the child and cmd exits, orphaning it out of this
        # tray's subtree; /b keeps it windowless (the pythonw child is windowless
        # anyway). CREATE_NO_WINDOW hides the transient cmd.
        cmd = [
            "cmd", "/c", "start", "", "/b",
            sys.executable, str(PROJECT_ROOT / "launcher.py"), "session-host",
            "--port", str(port),
        ]
        # Log to a file, not DEVNULL. The CLI configures logging with a
        # bare `logging.basicConfig()` — default handler, stderr — so pointing
        # stderr at DEVNULL silently discarded every session-host log line,
        # including the WARNING `run_git` emits explaining exactly why a build
        # identity failed to resolve. That is how this process ran for 8 days
        # reporting `git_sha: "unknown"` with the reason unrecoverable.
        # Appended, so a restart keeps the prior boot's context.
        try:
            SESSION_HOST_LOG.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(SESSION_HOST_LOG, "a", encoding="utf-8", errors="replace")
        except OSError as exc:
            # Never let a log target block the session-host from starting — the
            # PTY host matters more than its diagnostics.
            logger.warning(f"⚠️  session-host log unavailable ({exc}); falling back to DEVNULL")
            log_handle = None
        kw: dict = dict(
            cwd=str(PROJECT_ROOT),
            stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_handle is not None else subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        try:
            subprocess.Popen(cmd, **kw)
        except OSError as exc:
            logger.warning(f"⚠️  session-host failed to launch: {exc}")
            _notify("Session host", f"Failed to start: {exc}")
            return f"failed to launch the session-host: {exc}"
        finally:
            # The child inherited its own duplicate of the handle; close the
            # tray's copy so this long-lived process isn't pinning the file.
            if log_handle is not None:
                log_handle.close()
        logger.info(f"🧩 session-host spawned detached on :{port} (log: {SESSION_HOST_LOG})")
        return f"spawned a detached session-host on :{port}"

    def _stop_session_host(self) -> None:
        """Stop the session-host on an explicit Quit. It is detached (no Popen
        handle), so reclaim it by its owned port (see :func:`_reclaim_port`)."""
        port = _session_host_port()
        if not registered_trays.port_listening(port):
            return
        logger.info(f"🛑 Stopping session-host on :{port}")
        error = _reclaim_port(port)
        if error is not None:
            msg = f"{error} — session-host on :{port} may still be running"
            logger.warning(f"⚠️  {msg}")
            _notify("Session host stop failed", msg)

    # -- webapp lifecycle ----------------------------------------------------

    def _start(self) -> None:
        # Registered Trays are independent services that do not need this
        # webapp to be ready: a failed ready-wait (a slow cold-boot uvicorn
        # import past startup_timeout_seconds) must not skip them, and must
        # leave its reason in registered_trays.log.
        try:
            self.manager.start(wait=True)
            _notify("Launcher webapp ready", self.manager.base_url)
        except Exception as exc:  # noqa: BLE001
            self.starter_exc = exc
            logger.error(f"❌ webapp start failed: {exc}")
            registered_trays._log_breadcrumb(
                f"webapp start failed ({type(exc).__name__}: {exc}) "
                "— launching autostart entries anyway"
            )
            _notify("Launcher start failed", str(exc))
        try:
            registered_trays.launch_all()
        except Exception as exc:  # noqa: BLE001 — launch_all must never raise; belt-and-braces
            logger.error(f"❌ Registered Trays launch failed: {exc}")
            registered_trays._log_breadcrumb(f"launch_all raised: {exc}")

    def _on_webapp_wedge(self, failures: int) -> None:
        msg = (
            f"webapp on :{self.manager.config.port} stopped answering /healthz "
            f"({failures} consecutive probes, health={self.manager.probe_health()}, "
            f"{self._webapp_exit_detail()}) — self-heal restarts it if it stays down"
        )
        logger.error(f"❌ {msg}")
        _wd_log(f"WEDGE {msg}")
        _notify("Launcher webapp unresponsive", msg)

    def _on_webapp_recover(self) -> None:
        self.webapp_healer.recovered()
        msg = f"webapp on :{self.manager.config.port} answering /healthz again"
        logger.info(f"✅ {msg}")
        _wd_log(f"RECOVERED {msg}")
        _notify("Launcher webapp recovered", msg)

    def _webapp_exit_detail(self) -> str:
        code = self.manager.child_exit_code()
        if code is not None:
            return f"spawned webapp process exited with code {code}"
        if self.manager.status().ownership == OWNERSHIP_OURS:
            return "spawned webapp process still running"
        return "exit code unknown (no live child of this tray)"

    def _heal_webapp(self, failures: int) -> None:
        """Watchdog ``on_failing``: restart the webapp once it is due."""
        if not self.manager.config.enabled:
            return
        health = self.manager.probe_health()
        if not should_heal_webapp(health, failures, DEFAULT_FAILURES_TO_ALERT):
            return
        self.webapp_healer.attempt(
            f"{failures} failed probes, health={health}, {self._webapp_exit_detail()}"
        )

    def _restart_webapp_for_heal(self) -> str:
        """Free the webapp port the way tray.bat --restart does, then start fresh."""
        port = self.manager.config.port
        if self.manager.probe_health() == HEALTH_BOUND_NOT_ANSWERING:
            self.manager.stop()  # terminates a wedge this tray spawned
        if self.manager.probe_health() == HEALTH_BOUND_NOT_ANSWERING:
            error = _reclaim_port(port)
            if error is not None:
                raise RuntimeError(f"could not reclaim :{port}: {error}")
        status = self.manager.start(wait=True)
        if status.health != HEALTH_ANSWERING:
            raise RuntimeError(f"webapp started but {status.detail}")
        return f"done: {status.detail}"

    def _on_session_host_down(self, failures: int) -> None:
        msg = (
            f"session-host on :{_session_host_port()} not listening "
            f"({failures} consecutive probes; detached, so its exit code is "
            f"unknown here, see {SESSION_HOST_LOG.name}) — self-heal will respawn it"
        )
        logger.error(f"❌ {msg}")
        _wd_log(f"DOWN {msg}")
        _notify("Session host down", msg)

    def _on_session_host_recover(self) -> None:
        self.session_host_healer.recovered()
        msg = f"session-host on :{_session_host_port()} listening again"
        logger.info(f"✅ {msg}")
        _wd_log(f"RECOVERED {msg}")

    def _heal_session_host(self, failures: int) -> None:
        """Watchdog ``on_failing``: respawn a session-host that is gone.
        :meth:`_start_session_host` re-checks the port and adopts instead
        when one came back in the meantime, so a live one is never doubled."""
        self.session_host_healer.attempt(f"{failures} probes found nothing listening")

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
            creationflags=NO_WINDOW_NEW_GROUP,
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

    def copy_local(self, icon, item) -> None:  # noqa: ARG002
        webapp_cfg = load_webapp_config()
        url = append_auth_token(self.manager.base_url, webapp_cfg.auth_token)
        if _clipboard_copy(url):
            _notify("Copied local URL", url)
        else:
            _notify("Local URL", url)

    def copy_tailscale(self, icon, item) -> None:  # noqa: ARG002
        host = tailscale.resolve_hostname(_ts_debug)
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
        # The one state that must not read as a quieter kind of "up":
        # a wedged uvicorn still holds the port, so the Status item says so in
        # its own title rather than letting `detail` scroll past as normal.
        if s.health == HEALTH_BOUND_NOT_ANSWERING:
            _notify("Launcher status — NOT answering", f"{s.detail} · {s.base_url}")
            return
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
        for watchdog in (self.watchdog, self.session_host_watchdog):
            threading.Thread(
                target=watchdog.run, args=(self.watchdog_stop,), daemon=True
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

    # In-process single-instance guard: the tray.bat CIM
    # pre-check can let two near-simultaneous launches through, so the guarantee
    # must live in the process. Held for the tray's lifetime; the OS frees the
    # named mutex on exit.
    instance = SingleInstance(r"Global\app-launcher-tray")
    if not instance.acquired:
        logger.info("ℹ️  Another app-launcher tray is already running; exiting.")
        return 0

    return TrayApp(app_config, instance).run()
