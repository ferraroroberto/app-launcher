"""The GitHub host the Copilot CLI is signed in to, read from its own config.

A Copilot session's web view lives on the GitHub host of the account the
CLI runs as. The CLI records that account in ``~/.copilot/config.json`` as
``lastLoggedInUser: {host, login}`` the moment you sign in, ``host`` being
the account's web origin (``https://<host>``), so the host is read from
there rather than asked for again in a launcher setting. The file is JSON
behind a block of ``//`` comment lines; only whole-line comments are
stripped, and anything unreadable or oddly shaped answers ``None``.

Read on every sessions poll, so the parsed answer is memoized against the
file's ``(size, mtime)``: an unchanged file is stat'ed, not reopened.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_COPILOT_CONFIG = Path.home() / ".copilot" / "config.json"
# An https origin and nothing more: no path, query or credentials, so the
# value can head a URL as-is. A bare host name is accepted as https.
_ORIGIN_RE = re.compile(
    r"^(?:https://)?([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?)/?$"
)
# The file is a few KB; read bounded like every other reader here.
_CONFIG_CAP_BYTES = 256 * 1024

_memo: Tuple[Optional[Tuple[int, int]], Optional[str]] = (None, None)


def copilot_signed_in_host() -> Optional[str]:
    """The host name of the account the Copilot CLI last signed in with, or None."""
    global _memo
    try:
        st = _COPILOT_CONFIG.stat()
    except OSError:
        return None
    identity = (st.st_size, st.st_mtime_ns)
    if _memo[0] == identity:
        return _memo[1]
    host = _read_host(_COPILOT_CONFIG)
    _memo = (identity, host)
    return host


def _read_host(path: Path) -> Optional[str]:
    try:
        with path.open("rb") as fh:
            raw = fh.read(_CONFIG_CAP_BYTES).decode("utf-8", errors="replace")
    except OSError as exc:
        logger.info(f"ℹ️ Copilot config unreadable, no Copilot web host: {exc}")
        return None
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("//")
    )
    try:
        config = json.loads(body)
    except ValueError as exc:
        logger.info(f"ℹ️ Copilot config not parseable, no Copilot web host: {exc}")
        return None
    user = config.get("lastLoggedInUser") if isinstance(config, dict) else None
    origin = str(user.get("host") or "").strip() if isinstance(user, dict) else ""
    match = _ORIGIN_RE.match(origin)
    return match.group(1).lower() if match else None
