"""TEMPORARY diagnostic for #1041 - delete before merge.

Dumps the route table the guard-coverage tests inspect, from inside the same
fixture, so the CI log says what the runner actually sees.
"""

from __future__ import annotations

import sys

from tests.test_terminal_guard_coverage import (
    DELIBERATELY_TOKEN_ONLY,
    TERMINAL_PREFIXES,
    _terminal_route_paths,
    _under_terminal_prefix,
)


def test_ci_route_diagnostic(webapp_client):
    _client, app, _overrides = webapp_client
    import fastapi
    import starlette

    paths = sorted({getattr(r, "path", "") for r in app.routes})
    reg = _terminal_route_paths(app)
    lines = [
        f"python={sys.version}",
        f"fastapi={fastapi.__version__} starlette={starlette.__version__}",
        f"app_type={type(app)!r} routes_len={len(app.routes)}",
        f"route_types={sorted({type(r).__name__ for r in app.routes})}",
        f"terminal_prefixes={TERMINAL_PREFIXES!r}",
        f"under_prefix('/api/board')={_under_terminal_prefix('/api/board')}",
        f"registered_terminal_count={len(reg)}",
        f"registered_terminal={sorted(reg)}",
        f"allowlist_count={len(DELIBERATELY_TOKEN_ONLY)}",
        "ALL_PATHS:",
    ]
    lines += [f"  {p!r}" for p in paths]
    assert False, "\n".join(lines)
