"""CLI subcommand registry."""

from .base import BaseCommand
from .scan_cmd import ScanCommand
from .tray_cmd import TrayCommand
from .webapp_cmd import WebappCommand

COMMANDS = {
    "tray": TrayCommand,
    "webapp": WebappCommand,
    "scan": ScanCommand,
}


def get_command(name: str):
    return COMMANDS.get(name)


__all__ = ["BaseCommand", "COMMANDS", "get_command"]
