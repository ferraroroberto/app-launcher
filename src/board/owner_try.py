"""What an old owner check can show as its Try, taken from the issue's own words.

fleet-config's back-fill stamped one generic Try on every shipped-but-unchecked
issue. The Board never shows that boilerplate as an instruction: it quotes up to
:data:`MAX_LINES` checklist lines, verbatim, from the issue's acceptance
criteria, test plan or how-to-verify section, and says plainly when the issue
has none. Nothing is paraphrased, so nothing can be wrong that the issue is not.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

MAX_LINES = 3
LINE_MAX = 300
# Section names in priority order: the first one the issue has wins.
SECTIONS = ("acceptance criteria", "test plan", "how to verify")

# The back-fill's stored text opens with this clause; matching the opening keeps
# a later rewording of its tail from turning boilerplate into a "recorded" Try.
_GENERIC_OPENING = "use what this issue shipped once"

_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s+|\*\*|__)?([^*_#]+?)[\s*_:]*$")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(\S.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def is_generic(try_text: str) -> bool:
    """True when ``try_text`` is the back-fill boilerplate, not a written Try."""
    return " ".join(str(try_text or "").split()).lower().startswith(_GENERIC_OPENING)


def _section_name(line: str) -> Optional[str]:
    """The known section a heading line names, or None."""
    match = _HEADING.match(line)
    if not match:
        return None
    name = " ".join(match.group(1).split()).lower()
    return name if name in SECTIONS else None


def _sections(body: str) -> dict:
    """``{section name: [lines]}`` for the known sections, fenced code skipped."""
    found: dict = {}
    current: Optional[str] = None
    fenced = False
    for line in body.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        is_md_heading = bool(_MD_HEADING.match(line))
        name = _section_name(line) if (is_md_heading or line.lstrip().startswith(("**", "__"))) else None
        if name:
            current = name
            found.setdefault(name, [])
        elif is_md_heading:
            current = None
        elif current:
            found[current].append(line)
    return found


def derive(body: str) -> Optional[Tuple[str, List[str]]]:
    """``(section title, up to three checklist lines)`` quoted from ``body``, or
    None when the issue has no such section or it lists nothing."""
    sections = _sections(str(body or ""))
    for name in SECTIONS:
        lines = []
        for raw in sections.get(name, []):
            item = _ITEM.match(raw)
            if item:
                text = " ".join(item.group(1).split())
                lines.append(text if len(text) <= LINE_MAX else text[: LINE_MAX - 1].rstrip() + "…")
            if len(lines) == MAX_LINES:
                break
        if lines:
            return name.capitalize(), lines
    return None
