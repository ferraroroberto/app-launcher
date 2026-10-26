"""Guard: the shared date-math/formatting helpers stay in one place (#1791).

A `/codebase-audit` duplication bucket flagged, two audits running (recurring):
the "whole local calendar days between a date and today" scan written four
times (life-os.js's ``daysSince``/``convoWhen``, screen-time.js's ``whenText``,
jobs-agenda.js's ``_dayOffset``) instead of calling one
``calendarDaysFromToday`` in ``dom-utils.js``, and usage-meter.js's own
``fmtResetFull`` duplicating ``dom-utils.js``'s ``fmtResetClock``/
``fmtResetDay`` three ways over.

Both reinventions share one tell: a literal the shared helper already owns,
copy-pasted into a second file instead of an import. This scans every other
top-level ``app/webapp/static/*.js`` for either tell, so the next copy of
either helper fails the gate instead of waiting for the next audit to notice.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static"
_OWNER = "dom-utils.js"

# The day-in-milliseconds divisor: the one arithmetic constant a "days
# between two calendar dates" scan needs. It has no other legitimate use in
# this codebase's front end, so its mere presence outside dom-utils.js is a
# re-implementation of calendarDaysFromToday, not a coincidence.
_DAY_MS_RE = re.compile(r"86400000")

# A second ``fmtReset*`` formatter: the family dom-utils.js's fmtResetFull
# owns. A new one elsewhere is usage-meter.js's old fmtResetFull/fmtResetClock/
# fmtResetDay trio happening again.
_FMT_RESET_RE = re.compile(r"\bfunction\s+fmtReset\w*\s*\(")


def test_calendar_day_math_and_reset_formatters_stay_in_dom_utils() -> None:
    offenders = []
    for path in sorted(_STATIC.rglob("*.js")):
        if path.name == _OWNER or {"_vendored", "vendor"} & set(path.relative_to(_STATIC).parts):
            continue
        code = path.read_text(encoding="utf-8")
        if _DAY_MS_RE.search(code):
            offenders.append(
                f"{path.name}: reimplements a calendar-day-diff scan (found "
                f"86400000) — call calendarDaysFromToday() from {_OWNER} instead"
            )
        for match in _FMT_RESET_RE.finditer(code):
            offenders.append(
                f"{path.name}: defines {match.group(0).split('(')[0].strip()}(...) "
                f"— a second reset-stamp formatter beside fmtResetFull() in {_OWNER}"
            )
    assert not offenders, (
        "a shared date helper was reimplemented outside " + _OWNER + ":\n  "
        + "\n  ".join(offenders)
    )
