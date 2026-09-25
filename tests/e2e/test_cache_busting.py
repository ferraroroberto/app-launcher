"""Cache hygiene regression net (validates Part 1/5, commits 35caad4 + bf76d0d).

Issue #30's cache hygiene has four pillars: ``/`` is always revalidated,
``/static/*.{css,js}`` is immutable for a year, the ``?v=<hash>`` stamps in
``index.html`` match the on-disk fleet hash, and ``/api/version`` carries the
build-line keys. Three of those are plain response-header / JSON-shape
contracts pinned in-process by ``tests/test_webapp_api_basics.py``
(``test_index_is_no_cache``, ``test_js_served_immutable_year``,
``test_version_shape``) — their e2e copies were removed in #954.

What stays here is the one check only a *running* process can answer: the
``?v=<hash>`` stamped into the served ``index.html`` matches
``compute_asset_hashes(STATIC_DIR)`` — *this* is the test that catches
"forgot to invalidate after editing a JS file" because a stale stamp
diverges from the on-disk content's fleet hash.

Non-browser: uses ``requests`` against the live tray, so it runs once on the
chromium projection.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import requests

from src.static_versioning import compute_asset_hashes

pytestmark = pytest.mark.smoke

_STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static"
_INDEX_HREF_RE = re.compile(
    r"""(?:href|src)=['"]/static/(?P<name>[\w\-./]+\.(?:css|js))\?v=(?P<hash>[a-f0-9]+)['"]"""
)


def test_served_index_hashes_match_disk(base_url: str) -> None:
    """The single check that catches 'edited a JS file but didn't restart'."""
    res = requests.get(f"{base_url}/", verify=False, timeout=5)
    res.raise_for_status()
    served = dict(
        (m.group("name"), m.group("hash"))
        for m in _INDEX_HREF_RE.finditer(res.text)
    )
    assert served, "no hashed /static/*.{css,js} references found in served index.html"
    on_disk = compute_asset_hashes(_STATIC_DIR)
    for name, stamp in served.items():
        expected = on_disk.get(name)
        assert expected is not None, f"served index references {name} but it isn't on disk"
        assert stamp == expected, (
            f"{name}: served stamp {stamp!r} != fleet hash {expected!r} — "
            "the webapp's asset_hashes was computed against different bytes "
            "(tray needs restart, or a file changed under the running process)"
        )
