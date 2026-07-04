"""Shared atomic-JSON-write helper.

Six call sites across ``src/`` each hand-rolled the same
tempfile-then-``os.replace`` dance with their own suffix bikeshed. This
module is the single place that owns it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(target: Path, payload: Any, *, indent: int = 2) -> None:
    """Write ``payload`` as JSON to ``target`` atomically.

    Serializes to a sibling ``<name><suffix>.tmp`` file, then ``os.replace``s
    it over ``target`` — the swap is all-or-nothing, so a crash mid-write or
    a concurrent reader never observes a partially-written file. Caller is
    responsible for ensuring ``target.parent`` exists.
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=indent), encoding="utf-8")
    os.replace(tmp, target)
