"""Screen time's "By feature" counts: how often each input feature was used.

Arithmetic over the usage log's existing ``tap`` rows, like
:func:`src.usage_log.foreground_time`: no new column or event kind. The page
records one tap per successful use under a constant ``feat.*`` control id
(``static/settings/feature-track.js``), so a row holds an id, a device class
and a timestamp, never text, a file name or a transcript.

:data:`FEATURE_IDS` is the same list the page holds; a test pins the two equal.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, Tuple

from src.usage_log import format_ts

#: The ``data-track`` ids the page records, in the order the sheet lists them.
FEATURE_IDS: Tuple[str, ...] = (
    "feat.send.typed",
    "feat.send.dictated",
    "feat.dictation",
    "feat.attach.image",
    "feat.attach.file",
    "feat.attach.large",
    "feat.ocr",
    "feat.paste-pc",
)


def feature_counts(conn: sqlite3.Connection, since: datetime, until: datetime) -> Dict[str, Any]:
    """Uses per feature in ``[since, until]``: a total and a count per device
    class, every feature present (zero when unused)."""
    marks = ",".join("?" for _ in FEATURE_IDS)
    rows = conn.execute(
        "SELECT control_id, device_class, COUNT(*) FROM events"
        f" WHERE kind = 'tap' AND control_id IN ({marks}) AND ts >= ? AND ts <= ?"
        " GROUP BY control_id, device_class",
        (*FEATURE_IDS, format_ts(since), format_ts(until)),
    ).fetchall()
    by_device: Dict[str, Dict[str, int]] = {fid: {} for fid in FEATURE_IDS}
    for control_id, device_class, n in rows:
        by_device[str(control_id)][str(device_class)] = int(n)
    return {
        "since": format_ts(since),
        "until": format_ts(until),
        "features": [
            {"id": fid, "total": sum(by_device[fid].values()), "by_device": by_device[fid]}
            for fid in FEATURE_IDS
        ],
    }
