"""The open Board's automatic GitHub refresh: the client gate and the server backstop.

The gate (``static/board/github-refresh.js``) runs under plain Node; the
``auto`` guard is the route's (``{"auto": true}`` in the body), so two tabs aging out together cost one set
of ``gh`` searches while a tap always runs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.clients import github as github_client
from src.subprocess_flags import NO_WINDOW

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_auto_refresh_gate_age_and_hidden_page_node_pin():
    result = subprocess.run(
        ["node", str(_ROOT / "tests" / "js" / "board" / "github_refresh.test.mjs")],
        cwd=_ROOT, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_auto_refresh_skips_a_cache_filled_inside_the_cooldown(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client
    calls: list = []

    def _nothing_open(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", _nothing_open)
    first = client.post("/api/board/github/refresh", json={"auto": True}).json()
    assert first["fetched_at"] and calls, "a never-filled cache is refreshed"

    seen = len(calls)
    again = client.post("/api/board/github/refresh", json={"auto": True}).json()
    assert len(calls) == seen, "a second tab's automatic refresh inside the cooldown runs no gh"
    assert again["fetched_at"] == first["fetched_at"]

    client.post("/api/board/github/refresh")
    assert len(calls) > seen, "a tap on ↻ always runs"
