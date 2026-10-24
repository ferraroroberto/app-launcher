"""Assemble the SPA's served page from ``index.html`` and its view fragments.

``static/index.html`` keeps the shell (head, icon sprite, bottom-tab nav,
mount points, scripts); each view's markup lives in ``static/views/<name>.html``
and is pulled in where ``index.html`` carries a marker line on its own::

    <!-- @include views/board-pane.html -->

The marker line is replaced by the fragment's text verbatim, so the served
page is the same bytes a single-file ``index.html`` would give: no client-side
render, no first-paint gap, and every module's import-time ``getElementById``
still finds its element. Fragments do not nest.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Tuple

VIEWS_DIRNAME = "views"

# The whole line is the marker: leading whitespace is dropped (the fragment
# carries its own indentation) and so is the line's newline.
_INCLUDE_RE = re.compile(r"^[ \t]*<!-- @include (\S+) -->[ \t]*\n", re.MULTILINE)


def _fragment_path(index_path: Path, relpath: str) -> Path:
    base = index_path.parent.resolve()
    target = (base / relpath).resolve()
    if base / VIEWS_DIRNAME not in target.parents:
        raise ValueError(f"include {relpath!r} is outside {VIEWS_DIRNAME}/")
    return target


def assemble_shell(index_path: Path) -> str:
    """``index.html`` with every include marker replaced by its fragment."""

    def _sub(match: re.Match) -> str:
        return _fragment_path(index_path, match.group(1)).read_text(encoding="utf-8")

    return _INCLUDE_RE.sub(_sub, index_path.read_text(encoding="utf-8"))


def shell_stat_key(index_path: Path) -> Tuple[Tuple[int, int], ...]:
    """(mtime_ns, size) of ``index.html`` and every fragment, for a cache key.

    Globs the views dir rather than parsing the markers, so an edit, an added
    fragment or a removed one all move the key.
    """
    paths = [index_path, *sorted((index_path.parent / VIEWS_DIRNAME).glob("*.html"))]
    stats = [p.stat() for p in paths]
    return tuple((st.st_mtime_ns, st.st_size) for st in stats)
