"""No docstring or comment says work runs on the event loop's default executor.

Nothing under ``app/`` or ``src/`` submits to that pool (the request-budget
ratchet's ``default-pool`` rule, empty baseline), so prose that says a read
does, or that a caller wraps it in ``asyncio.to_thread``, names the wrong pool.
Not a ratchet: the premise is this repo's own, and there is no debt to burn.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# "as ``asyncio.to_thread`` does" compares behaviour; it claims no call.
CLAIM = re.compile(
    r"asyncio\.to_thread(?!`` does)|\bruns? on the (?:loop's )?default (?:executor|pool)"
)


def _claims(text: str) -> list[str]:
    # Prose wraps, so join lines (and comment markers) before matching.
    return CLAIM.findall(re.sub(r"\s*\n[ \t]*(?:#:?[ \t]*)?", " ", text))


def test_the_pattern_reads_wrapped_prose() -> None:
    assert _claims("reads run on the\n    default executor.")
    assert _claims("# callers wrap in\n# ``asyncio.to_thread``.")
    assert not _claims("as ``asyncio.to_thread`` does; off the default executor")


def test_no_prose_names_the_default_executor_as_where_work_runs() -> None:
    files = [p for top in ("app", "src") for p in (ROOT / top).rglob("*.py")]
    assert files, "no source scanned"
    found = {
        p.relative_to(ROOT).as_posix(): hits
        for p in files
        if (hits := _claims(p.read_text(encoding="utf-8")))
    }
    assert not found, f"name the pool that really runs it (app/webapp/route_pools.py): {found}"
