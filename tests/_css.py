"""The app's own stylesheet, as one string, in the order ``index.html`` links it.

The cascade is the link order, so a guard that reads a rule's declarations
reads the files the same way the browser stacks them.
"""

from __future__ import annotations

import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"

_STYLES_LINK = re.compile(r'<link rel="stylesheet" href="/static/(styles/[\w-]+\.css)')


def stylesheet_text() -> str:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    names = _STYLES_LINK.findall(index)
    assert names, "index.html links no styles/*.css"
    return "".join((STATIC / name).read_text(encoding="utf-8") for name in names).replace("\r\n", "\n")
