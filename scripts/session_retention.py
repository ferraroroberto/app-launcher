"""Report what the webapp/sessions retention sweep would remove (issue #902).

Dry-run only — this script never deletes anything. It runs the exact
selection the webapp's daily sweep runs (``src/session_retention.py::sweep``)
against the live session-host's session list and prints what would go and
why the rest stays. The retention window itself is
``session_retention_days`` in ``config/webapp_config.json``.

Exit status: 0 when a selection was made (even an empty one), 2 when the
sweep stood down because something it needs was unknown (session-host
unreachable, directory unreadable, clock suspect).

Usage
-----
    python scripts/session_retention.py                      # configured window, this checkout
    python scripts/session_retention.py --days 90            # try a different window
    python scripts/session_retention.py --dir <webapp/sessions> --show 20
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

from src import audit, session_retention  # noqa: E402
from src.webapp_config import load_webapp_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--dir", type=Path, default=None,
        help="sessions directory to inspect (default: this checkout's webapp/sessions)",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="retention window in days (default: session_retention_days from config)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="session-host port for the live list (default: from config)",
    )
    parser.add_argument(
        "--show", type=int, default=10,
        help="how many selected / unknown file names to print (default 10)",
    )
    args = parser.parse_args()

    cfg = load_webapp_config()
    days = cfg.session_retention_days if args.days is None else args.days
    if days < 1:
        print(f"ℹ️ retention is disabled (session_retention_days={days}); nothing is ever removed.")
        return 0
    port = cfg.session_host_port if args.port is None else args.port
    target = audit.sessions_dir() if args.dir is None else args.dir

    report = session_retention.sweep(
        target, days, session_retention.live_session_ids(port), dry_run=True
    )
    print(f"🔍 DRY RUN — {report.summary()}")
    if report.stood_down:
        return 2
    for path in report.expired[: args.show]:
        print(f"   would remove {path.name}")
    if len(report.expired) > args.show:
        print(f"   … and {len(report.expired) - args.show} more")
    for name, reason in report.unknown[: args.show]:
        print(f"   ⚠️ kept {name} — age unknown: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
