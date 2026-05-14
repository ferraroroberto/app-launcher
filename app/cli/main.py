"""CLI entry point — dispatches subcommands.

Examples:
    python launcher.py                 # same as `tray`
    python launcher.py tray
    python launcher.py webapp
    python launcher.py scan
"""

from __future__ import annotations

import argparse
import logging
import sys

from src import attach_app_log_handler, load_app_config

from .commands import COMMANDS, get_command

logger = logging.getLogger(__name__)


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    attach_app_log_handler()
    logging.getLogger().setLevel(level)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launcher",
        description="Phone-first launcher hub for Claude Code + Apps.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--log-level", default=None, help="Override log level (DEBUG/INFO/...)"
    )
    parser.add_argument(
        "--config", default=None, help="Path to config/config.json override"
    )

    subparsers = parser.add_subparsers(dest="command")
    for cmd_class in COMMANDS.values():
        cmd_class.add_parser(subparsers)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    app_config = load_app_config(args.config) if args.config else load_app_config()
    level = args.log_level or ("DEBUG" if args.debug else app_config.log_level)
    _configure_logging(level)

    if not args.command:
        args.command = "tray"

    command_cls = get_command(args.command)
    if command_cls is None:
        logger.error(f"❌ Unknown command: {args.command}")
        return 1

    try:
        return command_cls(app_config).execute(args)
    except KeyboardInterrupt:
        logger.warning("⏹️  Interrupted")
        return 130
    except Exception as e:
        if args.debug:
            raise
        logger.error(f"❌ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
