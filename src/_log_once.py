"""One log line per distinct key, with a bounded key set (issue #1003).

Three call sites had hand-rolled the same "log this key once, cap the set,
clear it wholesale on overflow" logic: ``board_transcript`` (unrecognized
title glyphs), ``board_sessions`` (suppressed state rows) and
``active_issue_claims`` (contract failures). All three exist for the same
reason — ``GET /api/board`` polls every five seconds, so a steady-state
condition would otherwise write the same line forever and bury the one that
matters.

**The cap is a parameter, not a shared constant.** #1003's finding proposed
"one helper with a shared cap"; the caps legitimately differ, because they
bound different key spaces:

* 64 for title glyphs — one entry per distinct leading glyph, a tiny set
  that is stable for a session's whole life;
* 512 for suppressed rows — one entry per session id, seen on a five-second
  poll, explicitly bounded so a long-lived tray cannot accumulate ids
  without limit.

Collapsing them to one number would be a behaviour change dressed as a
cleanup, so the caller keeps its own. What was genuinely duplicated is the
mechanism below — and ``active_issue_claims`` had no cap at all, which this
lets it fix by passing one.

Clearing wholesale on overflow (rather than evicting one entry) is the
existing behaviour of both capped sites, kept deliberately: these are
breadcrumbs, not a cache, and a cleared set simply means a condition may be
logged once more.
"""

from __future__ import annotations

from typing import Any, Callable, MutableSet


def log_once(
    seen: MutableSet[str],
    key: str,
    cap: int,
    emit: Callable[..., Any],
    msg: str,
    *args: Any,
) -> bool:
    """Emit ``msg % args`` the first time ``key`` is seen; return whether it did.

    ``seen`` is the caller's own module-level set (so each site keeps its own
    key space) and ``cap`` its own bound. ``emit`` is a bound logger method —
    ``logger.info``, ``logger.warning`` — passed in rather than a level name
    so the caller's logger, not this module's, owns the record.
    """
    if key in seen:
        return False
    if len(seen) >= cap:
        seen.clear()
    seen.add(key)
    emit(msg, *args)
    return True
