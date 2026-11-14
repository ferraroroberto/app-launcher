"""The owner alert for a client guessing the login password.

:class:`~src.login_lockout.LoginLockout` calls ``on_alert`` once per failure
streak (three wrong passwords in a row by default). This module turns that
call into one Telegram message, the same text in every fleet app::

    🚨 <app>: 3 wrong passwords in a row from 203.0.113.7 (+2 more held back)

It names the app, the client key and the failure count, never a password or a
token. Wiring, where the app builds its lock-out::

    from src.login_alert import build_login_alert
    lockout = LoginLockout(on_alert=build_login_alert("my-app", get_notifier))

``get_notifier`` is the app's own: it reads the app's existing config and
returns a notifier (anything with ``send_text(text)`` that raises on failure,
such as the vendored ``src.notify`` notifier), or ``None`` when no channel is
configured. It runs on the send thread, so a config read never delays the
login. Every alert ends in one of three outcomes, each logged as its own state:

- ``sent``: the notifier accepted the message.
- ``failed``: the notifier raised, or ``get_notifier`` did. The log names the
  error type only, since a transport error can carry its URL and token.
- ``unconfigured``: ``get_notifier`` returned ``None``. The alert text is
  logged at warning level so the attempt is still on record.

Vendor-verbatim (manifest key ``login_alert``), stdlib-only beyond the
vendored ``login_lockout``. Server side of
``app/webapp/static/_vendored/sign-in/README.md``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from src.login_lockout import LockoutAlert

logger = logging.getLogger(__name__)

SENT = "sent"
FAILED = "failed"
UNCONFIGURED = "unconfigured"


class AlertNotifier(Protocol):
    def send_text(self, text: str) -> None:
        """Deliver one plain-text message; raise on failure."""
        ...


NotifierSource = Callable[[], AlertNotifier | None]


def alert_text(app_name: str, alert: LockoutAlert) -> str:
    """The one alert line every fleet app sends."""
    held = f" (+{alert.suppressed} more held back)" if alert.suppressed else ""
    return f"🚨 {app_name}: {alert.failures} wrong passwords in a row from {alert.key}{held}"


def deliver_alert(app_name: str, alert: LockoutAlert, get_notifier: NotifierSource) -> str:
    """Send one alert now and return its outcome: ``sent``, ``failed`` or ``unconfigured``."""
    text = alert_text(app_name, alert)
    try:
        notifier = get_notifier()
        if notifier is None:
            logger.warning("⚠️ login alert unconfigured, not sent: %s", text)
            return UNCONFIGURED
        notifier.send_text(text)
    # Whatever the app's transport raises, the outcome is recorded and the
    # send thread ends cleanly.
    except Exception as exc:
        logger.warning("⚠️ login alert failed (%s): %s", type(exc).__name__, text)
        return FAILED
    logger.info("✅ login alert sent: %s", text)
    return SENT


def _start_thread(send: Callable[[], None]) -> None:
    threading.Thread(target=send, daemon=True, name="login-alert").start()


def build_login_alert(
    app_name: str,
    get_notifier: NotifierSource,
    *,
    run: Callable[[Callable[[], None]], None] = _start_thread,
) -> Callable[[LockoutAlert], None]:
    """The lock-out's ``on_alert``: hands the send to ``run`` so the login never waits on it."""

    def alert_owner(alert: LockoutAlert) -> None:
        def send() -> None:
            deliver_alert(app_name, alert, get_notifier)

        run(send)

    return alert_owner
