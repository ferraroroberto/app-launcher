"""Issue #725: every mounted router must be named in docs/architecture.mmd.

The diagram is hand-authored, and `CLAUDE.md` makes updating it in the same
PR as a material structural change a contract. `context_filter.py` shipped in
#713/#714 and the diagram was last touched one commit earlier, so it missed
the same-PR update by one — and because nothing checked, it stayed missed
until an audit read the two side by side.

A new mounted router is the single most common shape that contract covers and
the cheapest to verify mechanically, so it gets a check rather than a
convention. The rest of the diagram (process boundaries, the src/ library,
external deps) stays a judgement call no test can make — this deliberately
does not try.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = REPO_ROOT / "docs" / "architecture.mmd"
ROUTERS_DIR = REPO_ROOT / "app" / "webapp" / "routers"


def _mounted_routers() -> set[str]:
    """Router modules reachable from the app: mounted on the FastAPI app, or
    mounted on another router that is (``board_chief``, ``voice_ocr_tts``, the
    two ``jobs_*`` route modules)."""
    server = (REPO_ROOT / "app" / "webapp" / "server.py").read_text(encoding="utf-8")
    mounted = set(re.findall(r"app\.include_router\((\w+)\.router\)", server))
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        mounted |= set(
            re.findall(
                r"router\.include_router\((\w+)\.router\)",
                path.read_text(encoding="utf-8"),
            )
        )
    return mounted


def test_every_mounted_router_is_named_in_the_architecture_diagram():
    diagram = DIAGRAM.read_text(encoding="utf-8")
    missing = sorted(
        name for name in _mounted_routers() if f"{name}.py" not in diagram
    )
    assert missing == [], (
        "docs/architecture.mmd has drifted from the mounted routers — add "
        f"{missing} to its routers subgraph, in the same PR that mounted it "
        "(CLAUDE.md, 'Internal architecture')"
    )


def test_architecture_diagram_node_ids_are_unique():
    """A duplicated node id silently merges two boxes into one when Mermaid
    renders it, which reads as a structure that doesn't exist."""
    ids = re.findall(r"^\s{2,}([A-Za-z_][A-Za-z0-9_]*)\[", DIAGRAM.read_text(encoding="utf-8"), re.M)
    dupes = sorted({node for node in ids if ids.count(node) > 1})
    assert dupes == [], f"duplicate node ids in docs/architecture.mmd: {dupes}"
