"""Read the cloudflared config's first ingress hostname (issue #1003).

``app/tray/tray.py`` and ``scripts/run_named_tunnel.py`` each carried a copy
of this: same ``yaml.safe_load``, same walk over ``ingress[]``, same "a
missing or unparseable file means no tunnel" contract. The script exists for
headless / no-tray use, so it must **not** import the tray module to share
one function — hence a leaf both can depend on instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


def read_tunnel_hostname(config_path: Path) -> Optional[str]:
    """First ``ingress[].hostname`` in ``config_path``, or ``None``.

    ``None`` when the file is missing or unparseable — both callers treat
    that as "no tunnel" rather than an error: the tray skips spawning
    cloudflared, and the named-tunnel script still runs the tunnel but
    leaves ``last_tunnel_url.txt`` alone.
    """
    if not config_path.exists():
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"⚠️  Could not parse {config_path}: {exc}")
        return None
    for entry in data.get("ingress") or []:
        if isinstance(entry, dict) and entry.get("hostname"):
            return str(entry["hostname"]).strip()
    return None
