"""A Claude session's context-window use, read off its terminal's footer (#1223).

Nothing Claude Code writes to disk states how full a session's context
window is: the transcript's ``message.usage`` counts tokens but never names
the window size, so a percentage computed from it would be a guess. The one
place the real number shows is the fleet statusline Claude Code paints under
its prompt, from its own ``context_window.used_percentage``
(``fleet-config``'s ``statusline-command.ps1``)::

      27%c 10%s 79%w | opus | fleet-config (main)

``%c`` is the context window, ``%s`` the 5-hour window, ``%w`` the weekly
one, always in that order, each omitted when its value is null. Context use
is null early in a session and right after ``/compact``, so the footer then
starts at ``%s``. Probed on live captures rendered at 120 and 46 columns:
the footer keeps its leading usage segment at both widths (the script puts
usage first so it survives a narrow PTY), and at 120 columns Claude Code may
print its own text on the same row to the right (``... 316677 tokens``).

Only the bottom-most statusline row counts: an older frame or a quoted
footer higher up says nothing about now. A footer without ``%c`` is ``None``
rather than whatever an older row showed. If the statusline's format
changes, this reads ``None`` (the ring goes blank), never a wrong number.

Pure: the caller renders the screen (``src.plan_picker.screen_lines``).
"""

from __future__ import annotations

import re
from typing import List, Optional

# A statusline row: its first token is one of the three usage figures. The
# lookahead stops "5%status" or "12%cache" in ordinary text from reading as
# a footer.
_FOOTER_RE = re.compile(r"^\s*(\d{1,3})%([csw])(?![\w%])")


def context_percent(lines: List[str]) -> Optional[int]:
    """The context-window percentage the bottom-most statusline shows, or
    ``None`` when there is no statusline on screen or it shows no ``%c``."""
    for line in reversed(lines):
        m = _FOOTER_RE.match(line)
        if m:
            return int(m.group(1)) if m.group(2) == "c" else None
    return None
