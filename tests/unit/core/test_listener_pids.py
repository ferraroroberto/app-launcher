"""``diagnostics.listener_pids``: who holds a LISTEN socket, this process included."""

from __future__ import annotations

import os
import socket

from src import diagnostics


def test_names_this_process_for_a_port_it_listens_on():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert os.getpid() in (diagnostics.listener_pids(port) or [])


def test_a_port_nobody_listens_on_is_an_empty_list():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert diagnostics.listener_pids(port) == []


def test_an_unreadable_socket_table_is_none_not_empty(monkeypatch):
    def denied(**_kw):
        raise PermissionError("denied")

    monkeypatch.setattr(diagnostics.psutil, "net_connections", denied)
    assert diagnostics.listener_pids(8445) is None
