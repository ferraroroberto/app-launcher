"""Generate PWA/tray/Stream-Deck icons from the shared fleet icon-brand generator.

Thin caller onto ``project-scaffolding``'s ``brand_gen.render_set()`` — the
master art is app-launcher's vendored Lucide ``rocket.svg``, not a bespoke
Pillow-drawn silhouette (issue #65: a coherent icon family across the fleet).

Writes into ``app/webapp/static/``: ``icon-512.png``, ``icon-512-maskable.png``,
``icon-180.png``, ``icon-192.png``, ``favicon.ico``. Into ``assets/tray/``:
``app-launcher.ico``. Into ``assets/stream-deck/``: ``app-launcher-144.png``.

Usage:
    python scripts/gen_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SCAFFOLDING_SCRIPTS = Path(r"E:\automation\project-scaffolding\scripts")
sys.path.insert(0, str(SCAFFOLDING_SCRIPTS))

from brand_gen import render_set  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "app" / "webapp" / "static"


def main() -> None:
    render_set(
        master=Path(r"E:\automation\project-scaffolding\brand\rocket.svg"),
        out_dir=STATIC_DIR,
        tray_out_dir=PROJECT_ROOT / "assets" / "tray",
        stream_deck_out_dir=PROJECT_ROOT / "assets" / "stream-deck",
        project_slug="app-launcher",
    )
    print(f"wrote icons to {STATIC_DIR}")


if __name__ == "__main__":
    main()
