"""Every vendored web component is declared, or deliberately excluded (#1004).

`.fleet.toml`'s `[vendored]` manifest drives the fleet's byte-for-byte
re-vendor (`/propagate-vendored`): each entry pins the project-scaffolding
commit its copy came from. A component in the tree with no entry is
invisible to that pass — which is how six of seven ended up unmanaged.

The reverse error is worse. An entry asserts byte-for-byte provenance, so
declaring a component we have *locally modified* would let a re-vendor
overwrite the local work. `icons` is exactly that case and is excluded on
purpose; this test pins the exclusion so it stays a decision rather than
drifting back into an oversight.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDORED_DIR = _REPO_ROOT / "app" / "webapp" / "static" / "_vendored"

# Components deliberately left out of [vendored], with the reason they are
# not byte-identical to the scaffold. Adding to this list is a decision.
_EXCLUDED = {
    "icons": "local sprite carries app-launcher-only symbols (#1004)",
}


def _declared_sources() -> set[str]:
    text = (_REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8")
    block = text.split("[vendored]", 1)[1]
    # Stop at the next top-level table so [e2e] rules are never scanned.
    block = re.split(r"\n\[[a-z]", block, maxsplit=1)[0]
    return set(re.findall(r'src\s*=\s*"([^"]+)"', block))


def test_every_vendored_component_is_declared_or_excluded():
    components = sorted(p.name for p in _VENDORED_DIR.iterdir() if p.is_dir())
    assert components, "no vendored components found -- wrong path?"
    declared = _declared_sources()
    missing = [
        name
        for name in components
        if name not in _EXCLUDED
        and f"app/webapp/static/_vendored/{name}" not in declared
    ]
    assert not missing, (
        "vendored component(s) with no [vendored] entry, so the fleet's "
        "re-vendor pass cannot see them: " + ", ".join(missing)
    )


def test_excluded_components_are_not_declared():
    """An excluded component must stay undeclared: an entry would claim a
    byte-for-byte provenance it does not have, and a re-vendor would
    overwrite the local additions."""
    declared = _declared_sources()
    wrongly = [
        name
        for name in _EXCLUDED
        if f"app/webapp/static/_vendored/{name}" in declared
    ]
    assert not wrongly, (
        "component(s) declared in [vendored] despite being locally modified: "
        + ", ".join(f"{n} ({_EXCLUDED[n]})" for n in wrongly)
    )
