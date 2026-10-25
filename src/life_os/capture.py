"""Parse a Life OS conversation capture into Chat-pane transcript entries (#1119).

A capture is the markdown fleet-config's ``conversation_capture.render_markdown``
writes: a description line, an optional ``<!-- capture sid="…" agent="…" … -->``
header (legacy captures carry a shorter one, or none), then one block per
message — ``**You**: <text>`` or ``**Claude**:`` / ``**Codex**:`` /
``**Assistant**: <text>`` — separated by blank lines. Tool calls, tool output
and thinking are excluded by the capture reader upstream, so a capture only
ever yields ``user`` and ``assistant`` entries.

The entries use the shape the live ``/api/claude-code/sessions/{sid}/transcript``
route returns (``kind``/``text``/``offset``/``timestamp``/``truncated``), so the
Life OS viewer renders them through the session overlay's own Chat renderer
(``session-transcript.js``) rather than a second one (#979).

Only the four speaker labels delimit turns, and only at the start of a line
that follows a blank line (or opens the body): a reply routinely contains its
own ``**Recommended fix**:`` style headings, which must stay part of that reply.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

_HEADER_RE = re.compile(r"<!--\s*capture\b(?P<attrs>.*?)-->", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_SPEAKER_RE = re.compile(r"(?:\A|(?<=\n\n))\*\*(You|Claude|Codex|Assistant)\*\*: ?")
_AGENT_BY_LABEL = {"Claude": "claude", "Codex": "codex"}


def parse_capture(text: str) -> Dict[str, Any]:
    """Split one capture into ``{"agent": str, "entries": [...]}``.

    ``agent`` comes from the header when it names one, else from the first
    assistant speaker label; ``""`` when neither says. ``entries`` is empty
    when no speaker turn parses — the caller reports that as unreadable
    rather than as an empty conversation. ``offset`` is the label's character
    index in ``text``: stable for as long as the file is unchanged, which is
    all the renderer needs it for (keying disclosures).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    agent = ""
    body_start = 0
    header = _HEADER_RE.search(text)
    if header:
        attrs = dict(_ATTR_RE.findall(header.group("attrs")))
        agent = attrs.get("agent", "")
        body_start = header.end()
        # The body opens after the header's own blank line; skip it so the
        # first label sits at the start of the searched region.
        while body_start < len(text) and text[body_start] == "\n":
            body_start += 1
    body = text[body_start:]
    matches = list(_SPEAKER_RE.finditer(body))
    entries: List[Dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        message = body[match.end():end].strip("\n").rstrip()
        if not message.strip():
            continue
        label = match.group(1)
        if not agent and label in _AGENT_BY_LABEL:
            agent = _AGENT_BY_LABEL[label]
        entries.append({
            "kind": "user" if label == "You" else "assistant",
            "text": message,
            "offset": body_start + match.start(),
            "timestamp": None,
            "truncated": False,
        })
    return {"agent": agent, "entries": entries}
