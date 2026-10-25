"""Open a link on the PC in the browser the user works in (#1274).

Links in Chat and the transcript views are plain ``target=_blank`` anchors,
so they open in whatever browser hosts the page, and on the PC that is Edge:
the tray opens the app with the system default, and a session's mirror
window is an Edge ``--app`` window (#241). The page hands a link to
:func:`open_url` instead when it runs on the PC itself, which opens it in
Chrome when Chrome is installed and ``desktop_browser`` asks for it.

Chrome is found the way Windows finds it, through the registry's
``App Paths`` entry, never a hardcoded install path.
"""

from __future__ import annotations

import logging
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"


def find_chrome() -> Optional[str]:
    """Chrome's executable from ``App Paths`` (machine, then user), or None.

    None on a non-Windows host, with no registry entry, or when the entry
    names a file that no longer exists (an uninstall that left the key).
    """
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, _APP_PATHS_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = str(value or "").strip().strip('"')
        if path and Path(path).is_file():
            return path
    return None


def is_web_url(url: str) -> bool:
    """Only ``http``/``https`` with a host: never a ``file:``, script or app URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def open_url(url: str, preference: str) -> str:
    """Open ``url`` on the PC; return the browser used, ``"chrome"`` or ``"system"``.

    ``preference`` is ``webapp_config.desktop_browser``: ``"system"`` keeps the
    Windows default; anything else prefers Chrome and falls back to the
    default when Chrome isn't found or won't start. Raises ValueError for a
    URL :func:`is_web_url` refuses.
    """
    if not is_web_url(url):
        raise ValueError("only http(s) links open on the PC")
    chrome = find_chrome() if preference != "system" else None
    if chrome:
        try:
            subprocess.Popen([chrome, url], creationflags=NO_WINDOW, close_fds=True)
            logger.info("ℹ️ opened a link in Chrome on the PC")
            return "chrome"
        except OSError as exc:
            logger.warning("⚠️ Chrome would not start (%s); using the system browser", exc.__class__.__name__)
    webbrowser.open(url, new=2)
    logger.info("ℹ️ opened a link in the system browser on the PC")
    return "system"
