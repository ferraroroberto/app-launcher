"""``diagnostics.listener_pids``: who holds a LISTEN socket, this process included."""

from __future__ import annotations

import os
import socket

import psutil

from src import diagnostics


def _system_scan_readable() -> bool:
    try:
        psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError):
        return False
    return True


def _denied(*_a, **_kw):
    raise PermissionError("denied")


def test_names_this_process_for_a_port_it_listens_on():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert os.getpid() in (diagnostics.listener_pids(port) or [])


def test_a_port_nobody_listens_on_is_an_empty_list_when_the_table_is_readable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    answer = diagnostics.listener_pids(port)
    # A non-root macOS user cannot read every process's sockets, so "nobody
    # listens" is unknowable there and the contract is None, never [].
    assert answer == [] if _system_scan_readable() else answer in ([], None)


def test_an_unreadable_socket_table_is_none_not_empty(monkeypatch):
    class _Unreadable:
        pid = 1

        def net_connections(self, **_kw):
            raise psutil.AccessDenied(1)

    monkeypatch.setattr(diagnostics.psutil, "net_connections", _denied)
    monkeypatch.setattr(diagnostics.psutil, "process_iter", lambda: [_Unreadable()])
    assert diagnostics.listener_pids(8445) is None


def test_a_denied_table_still_names_a_listener_in_a_process_that_answers(monkeypatch):
    class _Addr:
        port = 8445

    class _Conn:
        status = psutil.CONN_LISTEN
        laddr = _Addr()

    class _Mine:
        pid = 4242

        def net_connections(self, **_kw):
            return [_Conn()]

    class _Unreadable:
        pid = 1

        def net_connections(self, **_kw):
            raise psutil.AccessDenied(1)

    monkeypatch.setattr(diagnostics.psutil, "net_connections", _denied)
    monkeypatch.setattr(
        diagnostics.psutil, "process_iter", lambda: [_Unreadable(), _Mine()]
    )
    assert diagnostics.listener_pids(8445) == [4242]


def test_a_denied_table_with_every_process_answering_is_an_empty_list(monkeypatch):
    class _Quiet:
        pid = 7

        def net_connections(self, **_kw):
            return []

    monkeypatch.setattr(diagnostics.psutil, "net_connections", _denied)
    monkeypatch.setattr(diagnostics.psutil, "process_iter", lambda: [_Quiet()])
    assert diagnostics.listener_pids(8445) == []
