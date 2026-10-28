"""`perf-summary` subcommand — the real-phone timings over the last N days.

Prints median and p95 per leg for page loads and slow API requests, per
device class, then the slowest API paths and which boot request lands last.
``--json`` prints the same report ``GET /api/perf/summary`` serves. Exit 6
when nothing has been recorded yet. How to read the legs:
``docs/perf-timings.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3

from src.runtime_data import runtime_db_path
from src.usage import perf_timings as perf

from .base import BaseCommand

logger = logging.getLogger(__name__)


def _cell(stats: dict) -> str:
    if not stats["n"]:
        return "unknown"
    return f"{stats['median']:.0f} / {stats['p95']:.0f}"


class PerfSummaryCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "perf-summary",
            help="Median and p95 per leg of the real-device load and slow-API timings",
        )
        p.add_argument("--days", type=int, default=7, choices=range(1, perf.MAX_SUMMARY_DAYS + 1),
                       metavar=f"1..{perf.MAX_SUMMARY_DAYS}", help="Window in days (default 7)")
        p.add_argument("--json", action="store_true", help="Print the raw report as JSON")

    def execute(self, args: argparse.Namespace) -> int:
        db_path = runtime_db_path("app-launcher", "perf.sqlite3")
        try:
            conn = perf.open_read_only(db_path)
        except FileNotFoundError:
            logger.error(f"❌ No timings recorded yet ({db_path} does not exist).")
            return 6
        try:
            report = perf.summarize(conn, args.days)
        except sqlite3.Error as exc:
            logger.error(f"❌ Could not read {db_path}: {exc}")
            return 1
        finally:
            conn.close()

        if args.json:
            print(json.dumps(report, indent=2))  # logging-not-print-exempt: --json is stdout data
            return 0
        logger.info(f"ℹ️ {report['rows']} row(s) since {report['since']}; builds: {', '.join(report['builds']) or '-'}")
        for title, key in (("Page loads", "loads"), ("Slow API requests", "api")):
            logger.info(f"{title} — median / p95 ms:")
            if not report[key]:
                logger.info("  none")
            for device, group in report[key].items():
                logger.info(f"  {device} ({group['rows']} rows)")
                for leg, stats in group["legs"].items():
                    logger.info(f"    {leg:14s} {_cell(stats)}")
        logger.info("Slowest API paths — total, server, network (median / p95 ms):")
        for p in report["slowest_paths"]:
            logger.info(f"  {p['path']} ({p['rows']}): {_cell(p['total'])}, {_cell(p['server'])}, {_cell(p['network'])}")
        logger.info("Request that landed last on boot:")
        for path, n in report["boot_last_paths"].items():
            logger.info(f"  {n:4d}  {path}")
        return 0
