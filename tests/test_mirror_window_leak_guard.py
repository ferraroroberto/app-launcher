"""Regression guard for issue #279 — non-e2e tests must never spawn a real PC
mirror window.

The Apps and Life OS launch handlers call ``open_local_terminal_window`` when
``should_mirror_to_pc`` is True, which it is under ``TestClient`` (its request
host isn't loopback). Left real, every launch test leaks an Edge ``--app``
window the test never closes. ``tests/conftest.py``'s autouse
``_no_real_mirror_window`` fixture stubs the symbol in both routers; if that
guard is ever removed, these tests fail loudly instead of silently leaking
windows again (nothing else asserts on desktop state).
"""

from __future__ import annotations

import pytest

from app.webapp.routers import apps as apps_router
from app.webapp.routers import life_os as life_os_router
from src import launcher


@pytest.mark.parametrize("router", [apps_router, life_os_router],
                         ids=["apps", "life_os"])
def test_router_mirror_symbol_is_stubbed(router):
    """The autouse fixture replaces each router's imported
    ``open_local_terminal_window`` with a no-op — so it is NOT the real
    spawner, and calling it spawns nothing."""
    assert router.open_local_terminal_window is not launcher.open_local_terminal_window
    # And it really is inert: a call reaches no window-spawning code.
    assert router.open_local_terminal_window("https://127.0.0.1/?terminal=s1", "s1") is None
