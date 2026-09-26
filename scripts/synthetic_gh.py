"""A fake ``gh`` for the design-review synthetic instance (#1286).

The synthetic instance (``scripts/design_review_synthetic.py``, #1227) points
``src.github_client``'s ``LAUNCHER_GH_CMD`` at this script, so the Board's
GitHub refresh reads the synthetic rows below instead of the real ``gh`` CLI,
whose login would otherwise put the real fleet's issue titles into a
throwaway walk's screenshots and report. It answers the three searches
``github_client`` runs (open issues, open PRs, issues closed since a date)
with the fields they ask for, and refuses anything else with a non-zero exit,
which the Board shows as a refresh error rather than as real data.

Run as ``python synthetic_gh.py <gh args>``; stdlib only, no network.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Dict, List

OWNER = "demo-owner"
REPO = "demo-project"


def _row(number: int, title: str, kind: str, labels: List[str], **extra: object) -> Dict[str, object]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "repository": {"name": REPO, "nameWithOwner": f"{OWNER}/{REPO}"},
        "number": number, "title": title,
        "url": f"https://example.invalid/{OWNER}/{REPO}/{kind}/{number}",
        "updatedAt": now, "labels": [{"name": name} for name in labels], **extra,
    }


OPEN_ISSUES = [
    _row(12, "Synthetic item in progress", "issues", ["enhancement"]),
    _row(13, "Synthetic item queued next", "issues", ["bug"]),
    _row(14, "A synthetic decision", "issues", ["chore"]),
]
OPEN_PRS = [_row(15, "Synthetic pull request", "pull", ["enhancement"], isDraft=False)]
CLOSED_TODAY = [_row(11, "Synthetic item finished today", "issues", ["bug"])]


def answer(args: List[str]) -> List[Dict[str, object]]:
    """The rows for one ``gh search`` call; ValueError for anything else."""
    if args[:1] != ["search"] or len(args) < 2:
        raise ValueError(f"synthetic gh answers only `gh search`, not: {' '.join(args[:2])}")
    state = args[args.index("--state") + 1] if "--state" in args else "open"
    if args[1] == "prs":
        return OPEN_PRS if state == "open" else []
    if args[1] == "issues":
        return OPEN_ISSUES if state == "open" else CLOSED_TODAY
    raise ValueError(f"synthetic gh has no answer for `gh search {args[1]}`")


def main(argv: List[str]) -> int:
    try:
        rows = answer(argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
