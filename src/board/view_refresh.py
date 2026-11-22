"""One server-side GitHub refresh for the open Board, however many tabs show it.

A Board tab that is up in a foreground page marks its ``GET /api/board`` poll
as a view (the ``X-Board-View`` header). On a view, :meth:`ViewRefresh.maybe_start`
starts the GitHub refresh (the ``gh`` searches, the owner checks and the CI
sweep) in one background thread when the cache is older than
:data:`REFRESH_TTL_SECONDS` or was never filled. The decision lives here, not
in each tab, so five open tabs cost the same one refresh per TTL as one, and
no refresh runs while nobody looks.

Single flight: a refresh already running starts no second one. One attempt
per TTL: a failed refresh keeps the cache's older stamp (the Board labels it
with the error), and the next attempt waits out the TTL instead of retrying
on every 5 s poll.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.board import ci_runs, owner_checks
from src.chief import ci_failed
from src.clients import github as github_client

logger = logging.getLogger(__name__)

#: How old the GitHub cache may get while a Board is open.
REFRESH_TTL_SECONDS = 300.0
#: The request header the Board's poll sends from a visible Board tab.
VIEW_HEADER = "X-Board-View"


class ViewRefresh:
    """The single-flight, once-per-TTL gate in front of the Board's refresh."""

    def __init__(
        self,
        *,
        ttl_seconds: float = REFRESH_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        spawn: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._spawn = spawn or _spawn_thread
        self._lock = threading.Lock()
        self._refresh_in_flight = False
        self._last_attempt: Optional[float] = None

    def maybe_start(self, cache_age_seconds: Optional[float], run: Callable[[], None]) -> bool:
        """Start ``run`` in the background if a refresh is due; ``True`` when it started.

        ``cache_age_seconds`` is the GitHub cache's age, ``None`` when it was
        never filled or its stamp does not parse (both are due).
        """
        tick = self._clock()
        with self._lock:
            if self._refresh_in_flight:
                return False
            if self._last_attempt is not None and tick - self._last_attempt < self._ttl:
                return False
            if cache_age_seconds is not None and cache_age_seconds < self._ttl:
                return False
            self._refresh_in_flight = True
            self._last_attempt = tick
        logger.info("ℹ️ Board view refresh: starting (cache age %s s)", cache_age_seconds)
        self._spawn(lambda: self._run(run))
        return True

    def _run(self, run: Callable[[], None]) -> None:
        try:
            run()
        except Exception:  # noqa: BLE001 — a failed refresh records its own error
            logger.exception("❌ Board view refresh failed")
        finally:
            with self._lock:
                self._refresh_in_flight = False


def _spawn_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="board-view-refresh", daemon=True).start()


def cache_age_seconds(fetched_at: Any) -> Optional[float]:
    """Seconds since ``fetched_at``; ``None`` when never filled or unparseable."""
    try:
        stamp = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def start_side_sweeps(cfg: Any) -> None:
    """Start the owner-check list's and the CI sweep's own background refreshes."""
    owner_checks.start_refresh(cfg.fleet_config_dir, cfg.github_owner)
    ci_runs.start_refresh(
        cfg.github_owner, cfg.fleet_config_dir, cfg.projects_dir,
        functools.partial(ci_failed.observe, cfg),
    )


def refresh_all(cfg: Any) -> None:
    """The open Board's refresh: the side sweeps, then the ``gh`` searches."""
    start_side_sweeps(cfg)
    github_client.refresh(cfg.github_owner)


def on_view(state: Any, cfg: Any) -> bool:
    """A visible Board polled: start the refresh if due; ``True`` when it started.

    The gate lives on ``state`` (the app's ``app.state``), so each app
    instance, and so each test's app, has its own.
    """
    gate = getattr(state, "board_view_refresh", None)
    if gate is None:
        gate = state.board_view_refresh = ViewRefresh()
    age = cache_age_seconds(github_client.snapshot().get("fetched_at"))
    return gate.maybe_start(age, lambda: refresh_all(cfg))
