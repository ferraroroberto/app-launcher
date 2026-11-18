"""Every e2e ``surface()`` name is an id in the UX surface map.

One id names a surface in ``docs/ux-surfaces.yaml``, in the markup's
``data-surface`` and in the e2e capture, so the design inventory speaks the
map's language. A name the map does not hold means a test walks a surface
nobody documented, or the map renamed an id and the walk did not follow.
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.ux_surface_map import load

REPO_ROOT = Path(__file__).resolve().parents[3]
SURFACE_MAP = REPO_ROOT / "docs" / "ux-surfaces.yaml"
E2E_DIR = REPO_ROOT / "tests" / "e2e"
_CALL_RE = re.compile(r"""(?<![\w.])surface\(\s*[A-Za-z_]+\s*,\s*["']([^"']+)["']""")


def test_every_e2e_surface_name_is_a_map_id() -> None:
    smap = load(SURFACE_MAP.read_text(encoding="utf-8"), "docs/ux-surfaces.yaml")
    map_ids = {surface.id for surface in smap.surfaces}
    assert map_ids, "no surface ids parsed from docs/ux-surfaces.yaml"

    unknown: dict[str, list[str]] = {}
    called = 0
    for path in sorted(E2E_DIR.rglob("*.py")):
        if path.name == "_surface.py":
            continue  # the vendored hook's docstring shows an example name
        for name in _CALL_RE.findall(path.read_text(encoding="utf-8")):
            called += 1
            if name not in map_ids:
                unknown.setdefault(name, []).append(path.name)

    assert called, "no surface() call found under tests/e2e"
    assert not unknown, f"e2e surface() names that are not ids in docs/ux-surfaces.yaml: {unknown}"
