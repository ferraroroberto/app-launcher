"""Failure notifications for Jobs-tab runs (issue #66, issue #597).

A small protocol surface + concrete Pushover/Telegram implementations;
the executor (:mod:`app.cli.commands.run_job_cmd`) calls
:func:`build_notifier_from_config` / :func:`build_telegram_notifier_from_config`
on finalisation and pushes when the run failed (or, optionally, when an
N-failure streak ticks over).

Two independent channels share this one finalisation hook:

* **Pushover (global, issue #66)** — every job, gated by the master
  switch. Config keys live in :class:`src.webapp_config.WebappConfig`:

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

* **Telegram (per-job, issue #597)** — only jobs with
  ``Job.alert_on_failure = True``, via the vendored :mod:`src.notify`
  Telegram primitive. Config keys:

  * ``telegram_bot_token`` / ``telegram_chat_id`` — credentials.
    Missing creds → :class:`NoopNotifier`.

  Opt-in per job (default off) so the shared Telegram chat isn't
  spammed by every job's failures.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

import requests

from src import llm_client
from src.notify import NotifierError as TelegramNotifierError
from src.notify import TelegramNotifier as _VendoredTelegramNotifier

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Local LLM hub — see global CLAUDE.md "claude-local-calls". The model id
# lives once on :mod:`src.llm_client` (:data:`llm_client.DEFAULT_MODEL`) —
# both hub callers route through the same client (issue #520).
LOCAL_LLM_BASE_URL = "http://127.0.0.1:8000"
LOCAL_LLM_TIMEOUT_SECONDS = 8.0
SUMMARY_TAIL_CHARS = 500

# Root-cause-focused system prompt for the failure-tail summary — distinct
# from llm_client's driving-mode reply summary, but asked through the same
# hub client instead of a second hand-rolled one.
_FAILURE_SUMMARY_SYSTEM_PROMPT = (
    "You are reviewing the tail of a failed job's stdout/stderr. Reply with "
    "ONE sentence (<= 25 words) describing the most likely root cause. No "
    "preamble."
)
_FAILURE_SUMMARY_MAX_TOKENS = 120


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


class TelegramNotifier:
    """Adapts the vendored :class:`src.notify.TelegramNotifier` to the
    ``Notifier`` protocol used by the executor (issue #597).

    ``title`` and ``body`` are joined into one plain-text message —
    Telegram has no separate subject line. Errors are logged and
    swallowed, same contract as :class:`PushoverNotifier`.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._notifier = _VendoredTelegramNotifier(bot_token, chat_id)

    def notify(self, title: str, body: str, severity: str = "warning") -> None:
        try:
            self._notifier.send_text(f"{title}\n\n{body}" if body else title)
        except TelegramNotifierError as exc:
            logger.warning(f"⚠️  telegram send failed: {exc}")


def summarise_failure(
    tail: str, *, base_url: Optional[str] = None
) -> Optional[str]:
    """Ask the local LLM hub for a one-line summary of ``tail``.

    Routes through the shared :func:`src.llm_client.summarize` hub client
    (issue #520) instead of hand-rolling a second one — same OpenAI-shape
    ``/v1/chat/completions`` call, model constant, and ``LlmError`` handling
    as the Coding-tab read-aloud summary, just with a root-cause-focused
    system prompt, a short (executor-safe) timeout, and a capped reply.

    ``base_url`` is the configured hub base URL (``WebappConfig.llm_hub_url``)
    — the caller threads it through so a user who moves the hub off ``:8000``
    still gets failure summaries. Falls back to :data:`LOCAL_LLM_BASE_URL`
    only when the config is genuinely missing/empty.

    Returns ``None`` when the hub is unreachable or the response is
    malformed — the caller falls back to the raw tail. Bounded by a
    short timeout so a wedged hub can't stall the executor's exit.
    """
    snippet = tail[-SUMMARY_TAIL_CHARS:] if tail else ""
    if not snippet.strip():
        return None
    base = (base_url or "").strip() or LOCAL_LLM_BASE_URL
    try:
        summary = llm_client.summarize(
            base,
            snippet,
            system_prompt=_FAILURE_SUMMARY_SYSTEM_PROMPT,
            max_tokens=_FAILURE_SUMMARY_MAX_TOKENS,
            timeout=LOCAL_LLM_TIMEOUT_SECONDS,
        )
    except llm_client.LlmError as exc:
        logger.debug(f"local LLM summary skipped: {exc}")
        return None
    return summary.splitlines()[0].strip() or None


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


def build_telegram_notifier_from_config(cfg: Any) -> Notifier:
    """Construct a Telegram :class:`Notifier` from a :class:`WebappConfig`-shaped object.

    Returns :class:`NoopNotifier` when either credential is missing —
    every caller can unconditionally ``notifier.notify(...)``.
    """
    bot_token = getattr(cfg, "telegram_bot_token", "") or ""
    chat_id = getattr(cfg, "telegram_chat_id", "") or ""
    if not (bot_token and chat_id):
        return NoopNotifier()
    return TelegramNotifier(bot_token, chat_id)
