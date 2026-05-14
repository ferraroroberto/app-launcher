"""Generate / rotate the webapp bearer token.

Why this exists
---------------
The webapp's Cloudflare tunnel puts the launcher on a public URL. Even
behind Cloudflare Access, the bearer token adds a second factor on the
API itself — a caller past the Access policy still needs the token to
trigger a launch.

Behaviour
---------
- With no ``auth_token`` set (the default): the gate is **off**. Every
  caller reaches the API freely.
- After running this script: the gate is **on**. Loopback callers
  still bypass — remote (tunnel) callers must present the token.

How the phone gets the token
----------------------------
The tray bakes it into the URL automatically. ``Tray -> Copy Cloudflare
URL`` appends ``?token=...`` to the URL it copies. Paste into the
phone's browser, open once, and the page extracts the token, stashes
it in localStorage, and strips it from the visible URL.

Rotation = re-run with --force, then re-open the new tokenised URL
once on each device that should keep working. Old devices stop working
immediately.

Usage
-----
    python scripts/gen_token.py            # generate iff none set
    python scripts/gen_token.py --force    # rotate even if one exists
    python scripts/gen_token.py --clear    # disable the gate
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.webapp_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    load_webapp_config,
    save_webapp_config,
)


def _generate() -> str:
    return secrets.token_urlsafe(32)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing auth_token",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="clear auth_token (disables the auth gate)",
    )
    args = parser.parse_args()

    cfg = load_webapp_config()
    if args.clear:
        cfg.auth_token = ""
        save_webapp_config(cfg)
        print(f"🧹 Cleared auth_token in {DEFAULT_CONFIG_PATH}")
        print("   The webapp's auth gate is now OFF.")
        return 0

    if cfg.auth_token and not args.force:
        print(
            f"ℹ️  auth_token is already set in {DEFAULT_CONFIG_PATH}.\n"
            f"   Re-run with --force to rotate, or --clear to disable."
        )
        return 0

    token = _generate()
    cfg.auth_token = token
    save_webapp_config(cfg)

    print()
    print("✅ Wrote a new auth_token to:")
    print(f"   {DEFAULT_CONFIG_PATH}")
    print()
    print("Token (also saved above — no need to copy):")
    print(f"   {token}")
    print()
    print("Restart the tray (or `tray.bat`) so uvicorn picks up the new value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
