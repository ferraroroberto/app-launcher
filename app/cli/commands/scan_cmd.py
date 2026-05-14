"""`scan` subcommand — one-shot registry scan from the CLI.

Useful when the user adds a new project on the box and wants to pick it
up without opening the web UI. Mirrors the POST /api/apps/scan +
POST /api/apps/save flow.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from src.registry import discover_new, load_registry, persist_additions
from src.webapp_config import load_webapp_config

from .base import BaseCommand

logger = logging.getLogger(__name__)


class ScanCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "scan",
            help="Scan for new Claude Code projects + app launchers and persist them",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="List new entries without writing config/apps.json",
        )

    def execute(self, args: argparse.Namespace) -> int:
        webapp_cfg = load_webapp_config()
        projects_dir = Path(webapp_cfg.projects_dir)
        scan_root = Path(webapp_cfg.apps_scan_root)
        registry = load_registry()

        new = discover_new(
            projects_dir=projects_dir,
            scan_root=scan_root,
            existing=registry,
        )

        if not new:
            logger.info("ℹ️ No new entries.")
            return 0

        logger.info(f"🔎 Found {len(new)} new entry(ies):")
        for entry in new:
            tail = entry.bat_path or entry.project_dir or ""
            logger.info(f"  • [{entry.kind}] {entry.name} — {tail}")

        if args.dry_run:
            logger.info("ℹ️ --dry-run: not persisting.")
            return 0

        added = persist_additions(registry, new, scan_root)
        logger.info(f"✅ Added {len(added)} entry(ies) to config/apps.json")
        return 0
