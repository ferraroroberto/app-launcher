"""`tray` subcommand — resident system-tray icon that owns webapp lifecycle."""

from __future__ import annotations

import argparse
import logging

from .base import BaseCommand

logger = logging.getLogger(__name__)


class TrayCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        subparsers.add_parser(
            "tray",
            help="Run resident in the system tray (owns the webapp process)",
        )

    def execute(self, args: argparse.Namespace) -> int:
        from app.tray.tray import run_tray  # lazy import — optional deps

        return run_tray(self.config)
