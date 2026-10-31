"""`usage-export` subcommand — send Screen time to task-os (#1678).

Sends the last 7 completed days and today's running total (or every day from
``--since``) as one task-os Time entry per day, skipping days that have not
changed since the last send; ``--dry-run`` prints the per-day table and sends
nothing. The nightly Job runs it bare. Exit codes, one per failure kind:
2 not set up, 3 category missing, 4 task-os unreachable, 5 task-os refused,
6 no usage log yet. The rules live in :mod:`src.usage.export`.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from src.usage.export import ExportError, run_export
from src.webapp_config import load_webapp_config

from .base import BaseCommand

logger = logging.getLogger(__name__)


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from None


class UsageExportCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "usage-export",
            help="Send Screen time to task-os, one entry per day (today included)",
        )
        p.add_argument(
            "--since",
            type=_day,
            default=None,
            help="First day to send (YYYY-MM-DD); default: the last 7 days and today",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the per-day minutes and send nothing",
        )

    def execute(self, args: argparse.Namespace) -> int:
        try:
            result = run_export(load_webapp_config(), since=args.since, dry_run=args.dry_run)
        except ValueError as exc:
            logger.error(f"❌ {exc}")
            return 2
        except ExportError as exc:
            logger.error(f"❌ {exc}")
            return exc.exit_code

        logger.info("  day          minutes  action  devices")
        for entry in result.entries:
            devices = ", ".join(f"{k} {v}" for k, v in entry.by_device.items())
            logger.info(f"  {entry.day.isoformat()}  {entry.minutes:7d}  {entry.action:6s}  {devices}")
        logger.info(f"  total        {result.total_minutes:7d}")
        if args.dry_run:
            logger.info("ℹ️ --dry-run: nothing sent.")
        else:
            logger.info(f"✅ {result.summary()}")
        return 0
