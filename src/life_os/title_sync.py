"""The outcome of mirroring a rename between a Life OS conversation and its sessions, declared once.

A rename on either side (the conversation's title on the Life OS tab, a
session's name on the Coding tab) is mirrored onto the other side, best
effort: the owner's rename always stands, and the reply carries this verdict
(``title_sync``, ``session_sync`` or ``life_os_sync``) so the phone can say
when the other side kept its old name.

* no verdict (``None``): there was nothing to mirror (no title, no target);
* ``synced``: every target took the name;
* ``failed``: at least one target kept its old name, or the targets could not
  be found; the verdict carries an ``error`` for the phone.

A partial mirror is ``failed``, never ``synced``: one stale name is what the
owner needs to hear about. ``docs/life-os-title-sync.md`` has the table.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional


class TitleSync(StrEnum):
    """Every value a title-sync verdict's ``status`` may hold."""

    #: Every target took the name.
    SYNCED = "synced"
    #: A target kept its old name, or the targets could not be found.
    FAILED = "failed"


def title_sync(synced: int, failed: int) -> Optional[TitleSync]:
    """The verdict for ``synced`` targets renamed and ``failed`` that refused; ``None`` for no target."""
    if failed:
        return TitleSync.FAILED
    if synced:
        return TitleSync.SYNCED
    return None
