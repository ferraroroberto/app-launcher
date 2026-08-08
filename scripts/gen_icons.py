"""Generate PWA/tray/Stream-Deck icons from the shared fleet icon-brand generator.

Thin caller onto ``project-scaffolding``'s ``brand_gen.render_set()`` — the
master art is app-launcher's vendored Lucide ``rocket.svg``, not a bespoke
Pillow-drawn silhouette (issue #65: a coherent icon family across the fleet).

Writes into ``app/webapp/static/``: ``icon-512.png``, ``icon-512-maskable.png``,
``icon-180.png``, ``icon-192.png``, ``favicon.ico``. Into ``assets/tray/``:
``app-launcher.ico``. Into ``assets/stream-deck/``: ``app-launcher-144.png``.

The ``project-scaffolding`` checkout is resolved as this repo's sibling, the
same convention ``src/webapp_config.py`` uses for the ``life-os`` and
``fleet-config`` checkouts. Set ``PROJECT_SCAFFOLDING_DIR`` to override when
it lives somewhere else.

Usage:
    python scripts/gen_icons.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _scaffolding_dir() -> Path:
    """The ``project-scaffolding`` checkout supplying the shared brand art.

    Derived from this repo's own location rather than hardcoded, so a clone
    on a different drive or directory layout still works — the sibling
    convention ``_default_life_os_dir`` / ``_default_claude_config_dir``
    already use. ``PROJECT_SCAFFOLDING_DIR`` overrides it for a checkout
    that isn't a sibling.
    """
    override = os.environ.get("PROJECT_SCAFFOLDING_DIR", "").strip()
    return Path(override) if override else PROJECT_ROOT.parent / "project-scaffolding"


SCAFFOLDING_DIR = _scaffolding_dir()
SCAFFOLDING_SCRIPTS = SCAFFOLDING_DIR / "scripts"
if not SCAFFOLDING_SCRIPTS.is_dir():
    raise SystemExit(
        f"project-scaffolding not found at {SCAFFOLDING_DIR} — clone it beside "
        "this repo, or point PROJECT_SCAFFOLDING_DIR at it."
    )
sys.path.insert(0, str(SCAFFOLDING_SCRIPTS))

from brand_gen import render_set  # noqa: E402

STATIC_DIR = PROJECT_ROOT / "app" / "webapp" / "static"


def main() -> None:
    render_set(
        master=SCAFFOLDING_DIR / "brand" / "rocket.svg",
        out_dir=STATIC_DIR,
        tray_out_dir=PROJECT_ROOT / "assets" / "tray",
        stream_deck_out_dir=PROJECT_ROOT / "assets" / "stream-deck",
        project_slug="app-launcher",
    )
    print(f"wrote icons to {STATIC_DIR}")


if __name__ == "__main__":
    main()
