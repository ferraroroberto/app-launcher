"""`life-os-zombie-cleanup` subcommand — back up or delete Life OS zombie conversations (#1738).

``--mode backup`` (the default) moves each zombie's capture, native transcript
and index entries into a dated backup folder; ``--mode delete`` removes them
through the Delete endpoint's own routine; ``--mode dry-run`` classifies and
prints counts, touching nothing and sending nothing. ``--restore <date>[/<skill>[/<file>]]``
lists what a backup holds, and puts it back with ``--yes``. Every non-dry run
sends one Telegram summary (counts only, to the log chat) and purges backups past
``--retention-days``. Exit codes: 1 a capture failed, 2 bad arguments,
3 breaker tripped (too many zombies; nothing touched), 4 session host unknown
(nothing touched; declare it ``deferred`` in the job). The rules live in
:mod:`src.life_os_zombies`.
"""

from __future__ import annotations

import argparse
import logging

from src.life_os_zombie_backup import (
    STATUS_RESTORED,
    backup_root,
    find_backups,
    restore_one,
)
from src.life_os_zombies import (
    DEFAULT_GRACE_HOURS,
    DEFAULT_MAX_PER_RUN,
    DEFAULT_RETENTION_DAYS,
    EXIT_FAILED,
    EXIT_OK,
    MODE_BACKUP,
    MODE_DRY_RUN,
    MODES,
    counts_lines,
    run_sweep,
    summary_text,
)
from src.notifications import TELEGRAM_CATEGORY_LOG, build_telegram_notifier_from_config
from src.webapp_config import WebappConfig, load_webapp_config

from .base import BaseCommand

logger = logging.getLogger(__name__)


class LifeOsZombieCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "life-os-zombie-cleanup",
            help="Back up or delete Life OS conversations nobody spoke in",
        )
        p.add_argument("--mode", choices=MODES, default=MODE_BACKUP,
                       help="backup (default): move to a dated backup; delete: remove; "
                            "dry-run: print counts only")
        p.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS,
                       help="Never touch a capture younger than this (default %(default)s)")
        p.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN,
                       help="Act on none when more zombies than this are found (default %(default)s)")
        p.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                       help="Purge backup folders older than this (default %(default)s)")
        p.add_argument("--restore", metavar="DATE[/SKILL[/FILE]]", default=None,
                       help="List a backup's conversations; add --yes to put them back")
        p.add_argument("--yes", action="store_true", help="With --restore: actually restore")

    def execute(self, args: argparse.Namespace) -> int:
        cfg = load_webapp_config()
        if args.restore:
            return self._restore(cfg, args.restore, apply=args.yes)
        if args.yes:
            logger.error("❌ --yes only applies to --restore")
            return 2
        result = run_sweep(
            cfg, mode=args.mode, grace_hours=args.grace_hours,
            max_per_run=args.max_per_run, retention_days=args.retention_days,
        )
        for row in counts_lines(result):
            logger.info("  %-24s %5d", row["label"], row["count"])
        if result.mode == MODE_DRY_RUN:
            logger.info("ℹ️ dry-run: nothing touched, nothing sent.")
            return result.exit_code
        title, body = summary_text(result)
        # Counts only: capture names embed the conversation's own words. A
        # routine summary, so the log chat, not alerts (#1742).
        build_telegram_notifier_from_config(cfg, category=TELEGRAM_CATEGORY_LOG).notify(
            title, body, "info" if result.exit_code == EXIT_OK else "warning")
        done = {EXIT_OK: "✅", EXIT_FAILED: "⚠️"}.get(result.exit_code, "❌")
        logger.info("%s %s: %s", done, title, body)
        return result.exit_code

    @staticmethod
    def _restore(cfg: WebappConfig, selector: str, *, apply: bool) -> int:
        root = backup_root()
        try:
            found = find_backups(root, selector)
        except ValueError as exc:
            logger.error(f"❌ {exc}")
            return 2
        if not found:
            logger.info("ℹ️ Nothing in the backup matches %s", selector)
            return EXIT_OK
        bad = 0
        for day, skill, capture in found:
            if not apply:
                logger.info("  %s/%s/%s", day, skill, capture)
                continue
            outcome = restore_one(cfg, root, day, skill, capture)
            if outcome.status == STATUS_RESTORED:
                logger.info("✅ restored %s/%s", skill, capture)
            else:
                bad += 1
                logger.warning("⚠️ %s %s/%s: %s", outcome.status, skill, capture, outcome.reason)
        if not apply:
            logger.info("ℹ️ %d conversation(s) in the backup; add --yes to restore them.", len(found))
        return EXIT_FAILED if bad else EXIT_OK
