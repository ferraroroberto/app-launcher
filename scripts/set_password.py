"""Set or clear the webapp's login password.

The bearer token gates every non-loopback API call. A password gives
the user a memorable secret to type instead of pasting a tokenised
URL — useful on iOS PWAs whose localStorage is partitioned from
Safari's main jar.

The password lives in ``config/webapp_config.json`` (gitignored)
alongside the bearer token. Failed attempts are logged with the
client IP to ``webapp/auth.log``.

Usage
-----
    python scripts/set_password.py <password>   # set or rotate
    python scripts/set_password.py --clear      # disable password gate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.webapp_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    load_webapp_config,
    save_webapp_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("password", nargs="?", help="the new password to set")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="clear auth_password (disables the password prompt)",
    )
    args = parser.parse_args()

    cfg = load_webapp_config()

    if args.clear:
        cfg.auth_password = ""
        save_webapp_config(cfg)
        print(f"🧹 Cleared auth_password in {DEFAULT_CONFIG_PATH}")
        print("   The password prompt is now OFF.")
        return 0

    if not args.password:
        parser.error("provide a password as the first argument, or use --clear")

    if not cfg.auth_token:
        print(
            "ℹ️  No auth_token is set yet — the password by itself does\n"
            "   nothing because /api/login hands back the bearer token.\n"
            "   Run `python scripts/gen_token.py` first, then re-run this."
        )
        return 1

    cfg.auth_password = args.password
    save_webapp_config(cfg)
    print(f"✅ Set auth_password (length {len(args.password)})")
    print(f"   Stored in: {DEFAULT_CONFIG_PATH}")
    print()
    print("Restart the tray. Open the webapp on a device with no token in")
    print("localStorage (e.g. iPhone PWA). The login overlay appears — type")
    print("the password, the server hands the bearer token back, the page")
    print("stashes it, you're in.")
    print()
    print("Failed attempts are logged with client IP to:")
    print(f"  {PROJECT_ROOT / 'webapp' / 'auth.log'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
