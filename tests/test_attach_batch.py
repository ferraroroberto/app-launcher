"""Unit pin for the composer's attach queue (issue #1354).

The queue, the size check and the summary wording are pure logic in
``app/webapp/static/attach-batch.js``, kept DOM-free so it can be pinned
without a browser. Like ``test_terminal_keys_bytes``, this shells out to plain
Node to import the real ES module and assert against it, in
``tests/js/attach_batch.test.mjs``; machines without Node skip cleanly.

It also pins the browser's copy of the upload limit to the session-host's:
the composer refuses an oversize file before sending a byte, so the two
constants drifting apart would either waste an upload (browser too lax) or
refuse a file the host would take (browser too strict).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.session_host import server as session_host_server

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_SCRIPT = _REPO_ROOT / "tests" / "js" / "attach_batch.test.mjs"
_MODULE = _REPO_ROOT / "app" / "webapp" / "static" / "attach-batch.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_attach_queue_size_check_and_summary():
    result = subprocess.run(
        ["node", str(_TEST_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "attach-batch.js failed its Node-side pin:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_browser_upload_limit_matches_the_session_host():
    source = _MODULE.read_text(encoding="utf-8")
    match = re.search(r"MAX_UPLOAD_BYTES\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024", source)
    assert match, "MAX_UPLOAD_BYTES not found in attach-batch.js"
    assert int(match.group(1)) * 1024 * 1024 == session_host_server._MAX_IMAGE_BYTES
