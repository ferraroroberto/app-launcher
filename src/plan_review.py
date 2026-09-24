"""Claude Code's ``ExitPlanMode`` tool, for the Chat pane's plan card (#1151).

Reading only. The call's input is ``{"plan": "<markdown>", ...}`` (sometimes
beside ``planFilePath`` / ``allowedPrompts``); its result says how the user
answered, in one of four shapes (characterized across 153 answered calls on
the dev box and re-probed on Claude Code 2.1.281):

* **approved** — not an error; ``toolUseResult`` is a dict carrying ``plan``
  and ``filePath`` (``planWasEdited`` when the user edited it first). It does
  not record *which* "Yes" option was picked.
* **sent back with feedback** — ``is_error``, the text "The user doesn't want
  to proceed with this tool use. … the user said:\\n<feedback>\\n\\nNote: …".
* **declined** — the same rejection without the "the user said" part (also
  what a session quit while the picker is up leaves behind).
* **never shown** — ``is_error`` with a ``<tool_use_error>``: the agent called
  the tool outside plan mode, so there was no picker.

Answering is not read from here: the pending call is not reliably in the
transcript while the picker is up, and the first option's meaning depends on
the session's permission mode. :mod:`src.plan_picker` reads the picker off
the terminal's screen instead (#1151).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

TOOL_NAME = "ExitPlanMode"

# Same budget as an assistant reply: a plan is the agent's prose.
PLAN_CAP = 12_000
FEEDBACK_CAP = 2_000

APPROVED = "approved"
SENT_BACK = "sent_back"
DECLINED = "declined"
NOT_SHOWN = "not_shown"

_FEEDBACK_RE = re.compile(r"the user said:\n(.*?)(?:\n\nNote:|\Z)", re.S)


def plan_from_input(inputs: Any) -> Optional[Tuple[str, bool]]:
    """``(plan markdown, truncated)``, or ``None`` when the input carries no
    plan text (the caller keeps the generic row)."""
    if not isinstance(inputs, dict):
        return None
    plan = inputs.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        return None
    text = plan.strip()
    return text[:PLAN_CAP], len(text) > PLAN_CAP


def plan_outcome(
    call_name: Optional[str], tool_use_result: Any, text: str, is_error: bool
) -> Optional[Dict[str, Any]]:
    """The answer's shape as entry fields (``plan_outcome`` and, for a plan
    sent back, ``plan_feedback``; ``plan_edited`` for an edited approval), or
    ``None`` when this result isn't a plan answer.

    An approval is recognisable on its own (``toolUseResult.plan``), so it
    closes a card whose call sits on an earlier page too; the error shapes
    are generic rejection text and are only read when the paired call is
    known to be ``ExitPlanMode``.
    """
    if not is_error and isinstance(tool_use_result, dict) and isinstance(tool_use_result.get("plan"), str) \
            and "filePath" in tool_use_result:
        out: Dict[str, Any] = {"plan_outcome": APPROVED}
        if tool_use_result.get("planWasEdited"):
            out["plan_edited"] = True
        return out
    if call_name != TOOL_NAME or not is_error:
        return None
    body = text or ""
    if "<tool_use_error>" in body:
        return {"plan_outcome": NOT_SHOWN}
    m = _FEEDBACK_RE.search(body)
    if m and m.group(1).strip():
        return {"plan_outcome": SENT_BACK, "plan_feedback": m.group(1).strip()[:FEEDBACK_CAP]}
    return {"plan_outcome": DECLINED}
