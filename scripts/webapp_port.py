"""Print a service port for the ``.bat``/``.ps1`` launchers.

The launchers read the port setting the Python code reads instead of carrying
a literal, so a PC that moves its ports edits only its ignored config:

* webapp: ``config/config.json`` ``webapp.port``, as the tray's webapp manager
  reads it (``app.webapp.manager.load_config``);
* session-host: ``config/webapp_config.json`` ``session_host_port``.

Prints only the port on stdout so ``for /f`` can capture it; a missing key or
file prints the default.

Usage (from the repo root):
    .venv\\Scripts\\python.exe -m scripts.webapp_port                  # webapp port
    .venv\\Scripts\\python.exe -m scripts.webapp_port --session-host   # session-host port
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from app.webapp.manager import load_config
from src.app_config import load_app_config
from src.webapp_config import load_webapp_config


def resolve_webapp_port() -> int:
    """The port the tray starts the webapp on."""
    return load_config(load_app_config().webapp).port


def resolve_session_host_port() -> int:
    """The loopback port the session-host binds."""
    return load_webapp_config().session_host_port


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.webapp_port", description=__doc__)
    parser.add_argument("--session-host", action="store_true", help="print the session-host port")
    args = parser.parse_args(argv)
    port = resolve_session_host_port() if args.session_host else resolve_webapp_port()
    sys.stdout.write(f"{port}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
