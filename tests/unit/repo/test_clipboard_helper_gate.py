"""Every copy button writes through ``writeClipboard`` in ``api.js``.

Browsers expose ``navigator.clipboard`` only on a secure origin, so a phone that
opens the app over plain HTTP gets ``undefined`` and a direct ``writeText`` call
throws a TypeError. The helper falls back to a selected hidden textarea and
``execCommand``; a call site that bypasses it crashes on HTTP.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static"
HELPER = STATIC / "api.js"
DIRECT_WRITE = re.compile(r"\bclipboard\s*\.\s*writeText\b")
THIRD_PARTY = ("vendor", "_vendored")


def _first_party_scripts() -> list[Path]:
    return [
        path
        for path in sorted(STATIC.rglob("*.js"))
        if path != HELPER and not any(part in THIRD_PARTY for part in path.relative_to(STATIC).parts)
    ]


def test_no_direct_clipboard_write_outside_the_helper() -> None:
    scripts = _first_party_scripts()
    assert scripts, "no static scripts found, so the scan proves nothing"
    offenders = [
        f"{path.relative_to(STATIC).as_posix()}:{number}"
        for path in scripts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if DIRECT_WRITE.search(line)
    ]
    assert not offenders, f"call writeClipboard from api.js, not clipboard.writeText: {offenders}"


def test_the_helper_is_exported_and_keeps_its_selection_fallback() -> None:
    source = HELPER.read_text(encoding="utf-8")
    assert "export async function writeClipboard(" in source
    assert "document.execCommand('copy')" in source
