"""`session-host` subcommand — run the loopback PTY session host.

This is the long-lived process that owns every launcher-spawned ``claude``
ConPTY. The tray starts and owns it automatically; this subcommand exists
for dev / headless runs.
"""

from __future__ import annotations

import argparse
import logging

from .base import BaseCommand

logger = logging.getLogger(__name__)


class SessionHostCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "session-host",
            help="Run the loopback PTY session host (owns claude ConPTYs)",
        )
        p.add_argument("--port", type=int, default=None, help="Override port")

    def execute(self, args: argparse.Namespace) -> int:
        from app.session_host.server import DEFAULT_PORT, run_session_host
        from src.webapp_config import load_webapp_config

        port = args.port or load_webapp_config().session_host_port or DEFAULT_PORT
        return run_session_host(port=port)
