"""The page's stylesheets as one response.

``index.html`` links 31 stylesheets in cascade order. Over HTTP/1.1 a browser
holds six connections per origin, so each extra request queues behind the
others on a phone. The source files stay separate for editing; at server
start they are concatenated, in the order ``index.html`` links them, into one
body served at ``/static/bundle.css`` and the served page links only that.

Concatenation keeps the cascade because it is the link order. The sheets use
no ``@import``/``@charset`` (which only work at the top of a sheet) and no
relative ``url()`` (which would resolve against the bundle's path), and
``tests/unit/static_ui/test_stylesheet_bundle.py`` pins both.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

BUNDLE_RELPATH = "bundle.css"

# Only the exact form index.html writes; any other <link> shape is left alone.
_LINK_RE = re.compile(
    r"""[ \t]*<link rel="stylesheet" href="/static/([\w\-./]+\.css)(?:\?v=[^"]*)?">[ \t]*\n?"""
)


def linked_stylesheets(shell: str) -> List[str]:
    """Static-relative paths of the stylesheet links in ``shell``, in link order."""
    return _LINK_RE.findall(shell)


def link_bundle(shell: str) -> str:
    """``shell`` with its stylesheet links replaced by one link to the bundle.

    The bundle link takes the first stylesheet link's place; the rest are
    dropped. A shell with no such links comes back unchanged.
    """
    seen = 0

    def _sub(match: re.Match) -> str:
        nonlocal seen
        seen += 1
        if seen > 1:
            return ""
        return f'  <link rel="stylesheet" href="/static/{BUNDLE_RELPATH}">\n'

    return _LINK_RE.sub(_sub, shell)


def build_bundle(static_dir: Path) -> Optional[str]:
    """The linked stylesheets of ``static_dir/index.html`` as one body.

    ``None`` when ``index.html`` links none. An unreadable sheet raises
    ``OSError``: serving a bundle that silently lacks one would restyle the page.
    """
    index = static_dir / "index.html"
    if not index.is_file():
        return None
    names = linked_stylesheets(index.read_text(encoding="utf-8"))
    if not names:
        return None
    parts = []
    for name in names:
        text = (static_dir / name).read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        parts.append(f"/* {name} */\n{text}")
    return "".join(parts)
