"""Thin launcher — the only entrypoint meant to be invoked by humans / .bat files.

Usage:
    python scripts/launcher.py                 # same as `tray` — day-to-day default
    python scripts/launcher.py tray
    python scripts/launcher.py webapp
    python scripts/launcher.py scan            # one-shot registry scan + persist

The launcher puts the repo root (its parent folder) on `sys.path` so the top-level packages
(`src`, `app`, `scripts`) resolve without any outer namespace. That lets
the folder be the root of its own repository — clone it,
`pip install -r requirements.txt`, run the launcher.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
