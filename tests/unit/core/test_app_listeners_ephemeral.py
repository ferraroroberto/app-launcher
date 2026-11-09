"""``list_app_listeners`` hides pywinpty's loopback ephemerals.

Every ``PtyProcess.spawn()`` opens a 127.0.0.1 listener on a random port in
the IANA dynamic range (49152 and up) that lingers after the PTY ends, so the
Running-apps panel listed the session host several times under bogus ports.
The filter drops loopback listeners in that range and keeps everything else.
``psutil`` is faked, so no real PTY or socket is needed; the route on top of
this (``GET /api/ports/probe``) is pinned in ``test_ports_probe_url.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import diagnostics

_LISTEN = "LISTEN"


def _conn(ip: str, port: int, pid: int) -> SimpleNamespace:
    return SimpleNamespace(status=_LISTEN, laddr=SimpleNamespace(ip=ip, port=port), pid=pid)


class _Proc:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def name(self) -> str:
        return "python.exe"

    def exe(self) -> str:
        return "python.exe"

    def cwd(self) -> str:
        return f"C:/stub/{self.pid}"

    def cmdline(self) -> list:
        return ["python"]

    def ppid(self) -> int:
        return 1


@pytest.fixture
def fake_psutil(monkeypatch):
    def install(conns):
        fake = SimpleNamespace(
            CONN_LISTEN=_LISTEN,
            AccessDenied=PermissionError,
            NoSuchProcess=LookupError,
            net_connections=lambda kind: conns,
            Process=_Proc,
        )
        monkeypatch.setattr(diagnostics, "psutil", fake)
        monkeypatch.setattr(diagnostics, "_psutil_ppid", lambda pid: None)

    return install


def test_loopback_ephemerals_are_hidden_and_real_listeners_kept(fake_psutil) -> None:
    fake_psutil([
        _conn("127.0.0.1", 8501, 10),     # a real app on loopback
        _conn("127.0.0.1", 49152, 11),    # the range's first port: pywinpty's
        _conn("127.0.0.1", 61234, 12),    # pywinpty's, deep in the range
        _conn("::1", 50000, 13),          # IPv6 loopback, same range
        _conn("0.0.0.0", 50001, 14),      # a high port bound on every interface
        _conn("127.0.0.1", 49151, 15),    # just below the range
    ])
    ports = [owner.port for owner in diagnostics.list_app_listeners()]
    assert ports == [8501, 49151, 50001]
