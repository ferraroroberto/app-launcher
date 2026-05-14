"""CLI subcommand registry."""

from .base import BaseCommand
from .scan_cmd import ScanCommand
from .session_host_cmd import SessionHostCommand
from .tray_cmd import TrayCommand
from .webapp_cmd import WebappCommand

COMMANDS = {
    "tray": TrayCommand,
    "webapp": WebappCommand,
    "scan": ScanCommand,
    "session-host": SessionHostCommand,
}


def get_command(name: str):
    return COMMANDS.get(name)


__all__ = ["BaseCommand", "COMMANDS", "get_command"]
