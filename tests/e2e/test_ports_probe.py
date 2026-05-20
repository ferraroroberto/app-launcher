"""Regression pin for commit d564114 (pywinpty loopback ephemerals hidden).

The bug: every ``PtyProcess.spawn()`` opens a 127.0.0.1 listener on a
random high port that lingers after the PTY ends, so the Running-apps
panel showed the session-host PID several times under bogus ports.
The fix filters out loopback listeners on ports >= 49152 in
``list_app_listeners()``.

This test exercises the user-facing surface (``GET /api/ports/probe``)
with a live pywinpty session running so a regression in the filter
shows up as a real row in the API response, not just a unit-level
failure inside ``src/diagnostics.py``.

Non-browser: ``requests`` against the live tray. Skips on the duplicate
projection so the suite total isn't inflated.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.smoke

# pywinpty opens its ephemerals in the dynamic/private range. The
# diagnostics filter uses the same threshold (IANA dynamic ports start
# at 49152), so this is the regression boundary.
_EPHEMERAL_THRESHOLD = 49152


@pytest.fixture(scope="session", autouse=True)
def _run_once(browser_name: str) -> None:
    if browser_name != "chromium":
        pytest.skip("server-side check; runs once on the chromium projection")


def test_probe_hides_pywinpty_ephemerals(
    base_url: str, auth_token: str, launched_pty_session: str
) -> None:
    # launched_pty_session has spawned a real claude PTY via the session-host,
    # so pywinpty's loopback ephemerals exist right now. The probe response
    # must NOT include them.
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    res = requests.get(
        f"{base_url}/api/ports/probe", headers=headers, verify=False, timeout=5
    )
    res.raise_for_status()
    listeners = res.json().get("listeners", [])

    ephemerals = [row for row in listeners if int(row.get("port", 0)) >= _EPHEMERAL_THRESHOLD]
    assert not ephemerals, (
        f"loopback ephemerals leaked into /api/ports/probe response: {ephemerals!r} — "
        "commit d564114's filter regressed"
    )
