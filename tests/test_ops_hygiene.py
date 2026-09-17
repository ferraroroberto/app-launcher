"""Hygiene pins for two operator-facing surfaces (issue #1002).

Both pins exist because the surface in question was reachable, or wrote its
output, more widely than its blast radius warranted:

* ``POST /api/ports/{port}/kill`` force-kills whatever process owns a port —
  including the ``:8446`` session host and every live PTY under it. It belongs
  in ``middleware._TERMINAL_GUARD_RULES`` with the same reachability
  requirement the rest of the high-blast-radius surface carries, not in the
  plain token-only tier.
* ``scripts/gen_token.py`` persists the value it generates before printing
  anything, so echoing that value again is optional output — and this script's
  documented invocation runs inside a launcher PTY session whose whole stdout
  is captured to a transcript retained for ``session_retention_days``
  (365 by default). Opt-in, not default.

Both tests fail against the pre-#1002 tree.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest


# ------------------------------------------------- port-kill reachability


def test_port_kill_is_in_the_guard_inventory():
    """The kill route classifies into the gated tier, like its neighbours."""
    from app.webapp.middleware import _terminal_guard_level

    assert _terminal_guard_level("/api/ports/8446/kill") == "tailnet"
    assert _terminal_guard_level("/api/ports/8445/kill") == "tailnet"


def test_port_probe_stays_token_only():
    """Only the destructive half moves tier — the read-only probe backs the
    Apps-tab listeners panel and must keep working on the token alone."""
    from app.webapp.middleware import _terminal_guard_level

    assert _terminal_guard_level("/api/ports/probe") is None


def test_port_kill_refused_off_tailnet(webapp_client):
    """The TestClient connects as host ``testclient`` — neither loopback nor
    tailnet — so the route must refuse outright rather than kill a process."""
    client, _, _ = webapp_client
    resp = client.post("/api/ports/8446/kill")
    assert resp.status_code == 403


# ------------------------------------------------- generated-token output


@pytest.fixture
def _stub_token_config(monkeypatch):
    """Drive ``gen_token.main`` against an in-memory config."""
    import scripts.gen_token as gen_token

    saved: list = []
    cfg = SimpleNamespace(auth_token="")
    monkeypatch.setattr(gen_token, "load_webapp_config", lambda: cfg)
    monkeypatch.setattr(gen_token, "save_webapp_config", saved.append)
    return gen_token, cfg, saved


def _run_gen_token(gen_token, argv: list) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gen_token.main()
    assert rc == 0
    return buf.getvalue()


def test_generated_token_is_not_echoed_by_default(_stub_token_config, monkeypatch):
    gen_token, cfg, saved = _stub_token_config
    monkeypatch.setattr(sys, "argv", ["gen_token.py"])

    out = _run_gen_token(gen_token, sys.argv)

    assert saved, "the token must still be persisted"
    assert cfg.auth_token, "a token must still be generated"
    assert cfg.auth_token not in out


def test_generated_token_is_echoed_on_explicit_opt_in(_stub_token_config, monkeypatch):
    gen_token, cfg, _ = _stub_token_config
    monkeypatch.setattr(sys, "argv", ["gen_token.py", "--show"])

    out = _run_gen_token(gen_token, sys.argv)

    assert cfg.auth_token in out
