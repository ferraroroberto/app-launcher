"""Guard: ``toast()`` is only ever called with a kind it recognises.

``api.js`` gives the error style, the assertive ``aria-live`` and the longer
display only to kind ``'error'``; any other kind string falls through as a
neutral 2.2 s toast, so a misspelt failure kind silently loses all three.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static"
_UNKNOWN_KIND_RE = re.compile(r"""toast\([^;\n]*,\s*'bad'""")


def test_no_toast_call_uses_the_unknown_bad_kind() -> None:
    offenders = [
        f"{path.relative_to(_STATIC)}:{n}"
        for path in _STATIC.rglob("*.js")
        if "_vendored" not in path.parts and "vendor" not in path.parts
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _UNKNOWN_KIND_RE.search(line)
    ]
    assert not offenders, f"toast kind 'bad' is not recognised; use 'error': {offenders}"
