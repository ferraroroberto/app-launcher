"""CLI subcommand registry."""

from .base import BaseCommand
from .life_os_zombie_cmd import LifeOsZombieCommand
from .run_job_cmd import RunJobCommand
from .scan_cmd import ScanCommand
from .session_host_cmd import SessionHostCommand
from .tray_cmd import TrayCommand
from .usage_export_cmd import UsageExportCommand
from .webapp_cmd import WebappCommand

COMMANDS = {
    "tray": TrayCommand,
    "webapp": WebappCommand,
    "scan": ScanCommand,
    "session-host": SessionHostCommand,
    "run-job": RunJobCommand,
    "usage-export": UsageExportCommand,
    "life-os-zombie-cleanup": LifeOsZombieCommand,
}


def get_command(name: str):
    return COMMANDS.get(name)


__all__ = ["BaseCommand", "COMMANDS", "get_command"]
