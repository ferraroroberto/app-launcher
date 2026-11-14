"""The usage meters' quota rows, kept in memory while ``/api/rate-limits`` is polled.

The shard read loads the fleet contract and walks the shard directory; on a
saturated box it stalls for seconds, and the 5 s meter poll paid that on every
request. :func:`rate_limits_cache` builds the rows off the request path (a
:class:`app.webapp.demand_cache.DemandCache` the lifespan ticks while the route
is read), so a poll is a memory read. The collectors write the shards from other
processes, so a snapshot is at most ``max_age_s`` behind.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

from app.webapp.demand_cache import DemandCache
from src import board
from src.usage import quota as quota_usage
from src.webapp_config import WebappConfig

# How often the shards are re-read while the meters are polled.
RATE_LIMITS_TICK_S = 5.0


def read_quota_lines(cfg: WebappConfig) -> List[Dict[str, Any]]:
    """Both heavy agents, always, independent of the current selection."""
    legacy_path = Path(cfg.rate_limits_file)
    return quota_usage.read_quota_lines(
        Path(cfg.fleet_config_dir),
        legacy_path.parent,
        legacy_reader=lambda: board.read_rate_limits(legacy_path),
    )


def rate_limits_cache(app: Any) -> DemandCache[List[Dict[str, Any]]]:
    """The app's quota-rows snapshot."""
    cache = getattr(app.state, "rate_limits_cache", None)
    if cache is None:
        cache = app.state.rate_limits_cache = DemandCache(
            "GET /api/rate-limits",
            lambda: asyncio.to_thread(read_quota_lines, app.state.webapp_config),
            tick_s=RATE_LIMITS_TICK_S,
            max_age_s=3 * RATE_LIMITS_TICK_S,
        )
    return cache
