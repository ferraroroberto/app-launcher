"""Fixtures for the Board drill-down tests: the passkey gate bypassed."""

from __future__ import annotations


import pytest



@pytest.fixture
def _bypass_gate(monkeypatch):
    """Treat the TestClient host as loopback so the gated proxy logic is
    exercised (the gate itself is covered by TestGateRefusal)."""
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware.trust,
        "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )
