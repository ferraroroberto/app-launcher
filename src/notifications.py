"""Failure notifications for Jobs-tab runs (issue #66).

A small protocol surface + concrete Pushover implementation; the
executor (:mod:`app.cli.commands.run_job_cmd`) calls
:func:`build_notifier_from_config` on finalisation and pushes when the
run failed (or, optionally, when an N-failure streak ticks over).

Config keys live in :class:`src.webapp_config.WebappConfig`:

* ``pushover_api_token`` / ``pushover_user_key`` — credentials.
  Missing creds → :class:`NoopNotifier`.
* ``notify_on_failure`` — master switch (default off, so the feature
  ships dormant until the user opts in).
* ``notify_failure_streak`` — extra fire when the consecutive-failure
  count equals this value (0 = disabled).
* ``notify_failure_summary`` — when true, pipe the output tail through
  the local LLM hub (``http://127.0.0.1:8000``, ``claude-haiku-4-5``)
  for a one-line "what went wrong" summary prepended to the push body.
  Hub unreachable → silently falls back to the raw tail.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

import requests

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Local LLM hub — see global CLAUDE.md "claude-local-calls".
LOCAL_LLM_BASE_URL = "http://127.0.0.1:8000"
LOCAL_LLM_MODEL = "claude-haiku-4-5"
LOCAL_LLM_TIMEOUT_SECONDS = 8.0
SUMMARY_TAIL_CHARS = 500


class Notifier(Protocol):
    """Minimal push-notification surface — see :class:`PushoverNotifier`."""

    def notify(self, title: str, body: str, severity: str) -> None: ...


class NoopNotifier:
    """No-op notifier — used when credentials are not configured."""

    def notify(self, title: str, body: str, severity: str) -> None:
        return None


class PushoverNotifier:
    """POST to Pushover. Errors are logged and swallowed.

    ``severity`` maps to Pushover ``priority``:

    * ``"info"``    →  -1 (low/no sound)
    * ``"warning"`` →   0 (normal)
    * ``"error"``   →   1 (high — bypass quiet hours)
    """

    _PRIORITY = {"info": -1, "warning": 0, "error": 1}

    def __init__(
        self,
        api_token: str,
        user_key: str,
        *,
        http: Any = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._api_token = api_token
        self._user_key = user_key
        self._http = http or requests
        self._timeout_seconds = timeout_seconds

    def notify(self, title: str, body: str, severity: str = "warning") -> None:
        # Pushover caps message length at ~1024 chars; truncate so the
        # tail of long failures doesn't get dropped by Pushover itself.
        max_message = 1024
        message = body if len(body) <= max_message else body[: max_message - 1] + "…"
        payload = {
            "token": self._api_token,
            "user": self._user_key,
            "title": title[:250],
            "message": message,
            "priority": self._PRIORITY.get(severity, 0),
        }
        try:
            resp = self._http.post(
                PUSHOVER_URL, data=payload, timeout=self._timeout_seconds
            )
            if not (200 <= resp.status_code < 300):
                logger.warning(
                    f"⚠️  pushover non-2xx: rc={resp.status_code} "
                    f"body={resp.text[:200]!r}"
                )
        except Exception as exc:  # noqa: BLE001 — exec-side: never raise
            logger.warning(f"⚠️  pushover send failed: {exc}")


def summarise_failure(tail: str, *, http: Any = None) -> Optional[str]:
    """Ask the local LLM hub for a one-line summary of ``tail``.

    Returns ``None`` when the hub is unreachable or the response is
    malformed — the caller falls back to the raw tail. Bounded by a
    short timeout so a wedged hub can't stall the executor's exit.
    """
    snippet = tail[-SUMMARY_TAIL_CHARS:] if tail else ""
    if not snippet.strip():
        return None
    client = http or requests
    body = {
        "model": LOCAL_LLM_MODEL,
        "max_tokens": 120,
        "messages": [
            {
                "role": "user",
                "content": (
                    "You are reviewing the tail of a failed job's stdout/"
                    "stderr. Reply with ONE sentence (<= 25 words) "
                    "describing the most likely root cause. No preamble.\n\n"
                    f"---\n{snippet}\n---"
                ),
            }
        ],
    }
    try:
        resp = client.post(
            f"{LOCAL_LLM_BASE_URL}/v1/messages",
            json=body,
            headers={"x-api-key": "local-dummy", "anthropic-version": "2023-06-01"},
            timeout=LOCAL_LLM_TIMEOUT_SECONDS,
        )
        if not (200 <= resp.status_code < 300):
            return None
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"local LLM summary skipped: {exc}")
        return None
    # Anthropic shape: { content: [{type:"text", text:"…"}, …] }
    try:
        blocks = data.get("content") or []
        for block in blocks:
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    return text.splitlines()[0].strip()
    except (AttributeError, TypeError):
        return None
    return None


def build_notifier_from_config(cfg: Any) -> Notifier:
    """Construct a Notifier from a :class:`WebappConfig`-shaped object.

    Returns :class:`NoopNotifier` when creds or the master switch are
    missing — every caller can unconditionally ``notifier.notify(...)``.
    """
    api_token = getattr(cfg, "pushover_api_token", "") or ""
    user_key = getattr(cfg, "pushover_user_key", "") or ""
    if not (api_token and user_key):
        return NoopNotifier()
    return PushoverNotifier(api_token, user_key)
