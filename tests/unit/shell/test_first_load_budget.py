"""No first-load response over 100 KB on the wire.

The page a phone opens downloads the served shell, the stylesheet bundle and
every module ``main.js`` imports statically, before a list can draw. Each of
them must stay under the budget gzipped (the server compresses them), so no
single request holds up the first paint on a slow link. A lazy ``import()``
is not first-load and is not counted.
"""

from __future__ import annotations

import gzip
import re
from collections import deque
from pathlib import Path
from typing import Dict

from app.webapp.shell_html import assemble_shell
from src.compression import COMPRESS_LEVEL
from src.stylesheet_bundle import build_bundle, link_bundle

STATIC_DIR = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static"
BUDGET_BYTES = 100 * 1024

# A static import or re-export; `import(` (lazy) never matches.
_IMPORT_RE = re.compile(
    r"""^\s*(?:import|export)\s[^'"]*?from\s*['"](\.[^'"]+)['"]|^\s*import\s*['"](\.[^'"]+)['"]""",
    re.M,
)


def _gz(text: str) -> int:
    # The server's own level, so the count matches the bytes on the wire.
    return len(gzip.compress(text.encode("utf-8"), compresslevel=COMPRESS_LEVEL))


def _module_graph() -> Dict[Path, str]:
    entry = STATIC_DIR / "main.js"
    seen: Dict[Path, str] = {}
    queue = deque([entry])
    while queue:
        path = queue.popleft()
        if path in seen:
            continue
        text = path.read_text(encoding="utf-8")
        seen[path] = text
        for match in _IMPORT_RE.finditer(text):
            queue.append((path.parent / (match.group(1) or match.group(2))).resolve())
    return seen


def test_module_graph_is_walked() -> None:
    graph = _module_graph()
    # A walker that silently found nothing would pass every budget below.
    assert len(graph) > 50
    assert STATIC_DIR / "sessions.js" in graph
    assert STATIC_DIR / "shared" / "boot-snapshot.js" in graph


def test_served_shell_is_under_budget() -> None:
    shell = link_bundle(assemble_shell(STATIC_DIR / "index.html"))
    assert _gz(shell) < BUDGET_BYTES


def test_stylesheet_bundle_is_under_budget() -> None:
    bundle = build_bundle(STATIC_DIR)
    assert bundle is not None
    assert _gz(bundle) < BUDGET_BYTES


def test_every_first_load_module_is_under_budget() -> None:
    over = {
        str(path.relative_to(STATIC_DIR)): size
        for path, text in _module_graph().items()
        if (size := _gz(text)) >= BUDGET_BYTES
    }
    assert not over, f"first-load modules over {BUDGET_BYTES} B gzipped: {over}"
