"""The transcript endpoints skip the passkey gate when a test asks for _bypass_gate."""

from __future__ import annotations

import pytest


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )
