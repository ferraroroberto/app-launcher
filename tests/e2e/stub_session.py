"""The lightweight PTY stub the e2e gate launches instead of the real Claude CLI.

Moved out of ``tests/e2e/conftest.py`` unchanged (app-launcher#1227) so the
design-review synthetic instance (``scripts/design_review_synthetic.py``)
launches the same deterministic child the gate does, rather than a copy.
No pytest import: a plain script can use it.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Sentinel flag for the lightweight PTY child (issue #534). Under autoboot the
# disposable session-host's PATH is prepended with a harness-generated
# `claude.cmd` shim: a launch whose flags are exactly this sentinel runs a
# tiny deterministic Python echo loop instead of the real Claude CLI (3-5 s
# startup each), while any other flag set falls through to the real `claude`.
# Purely a harness substitution — no production code knows about it.
STUB_FLAG = "--e2e-stub"
# The banner that child prints on startup — the marker that identifies a
# transcript as harness-written (issue #913's isolation check).
STUB_BANNER = "[e2e-stub]"


STUB_CHILD_SOURCE = '''\
"""Deterministic lightweight PTY child for UI-only e2e tests (issue #534).

Stands in for the real Claude CLI under the disposable autoboot session-host:
instant startup, echoes each input line (ConPTY cooked mode echoes keystrokes
too), exits on /quit so the host's graceful stop path works.
"""
import sys

print("[e2e-stub] lightweight PTY child ready (issue #534)", flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    text = line.rstrip("\\r\\n")
    if text.strip() == "/quit":
        print("[e2e-stub] bye", flush=True)
        break
    print(text, flush=True)
'''

# Drift guard: the e2e conftest's `_leaked_stub_sessions` identifies a harness-written transcript
# by this banner, so the child it comes from must actually print it.
assert STUB_BANNER in STUB_CHILD_SOURCE


def write_claude_shim(shim_dir: Path) -> None:
    """Generate the `claude.cmd` PATH shim + stub child script (issue #534).

    The session-host spawns agents via ``cmd /c … && claude <flags>`` with the
    command resolved off its own PATH, so prepending this directory to the
    *disposable* session-host's PATH intercepts every claude launch: the
    ``--e2e-stub`` sentinel routes to the stub child, anything else falls
    through to the real ``claude`` resolved at generation time. Where claude
    isn't installed (the CI runner) the fall-through branch fails loud — but
    it is never reached there, because `launched_claude_pty_session` skips
    first (same `shutil.which` guard as always).
    """
    stub_py = shim_dir / "e2e_stub_child.py"
    stub_py.write_text(STUB_CHILD_SOURCE, encoding="utf-8")
    real_claude = shutil.which("claude")
    if real_claude:
        real_branch = f'call "{real_claude}" %*\nexit /b %ERRORLEVEL%\n'
    else:
        real_branch = (
            "echo [e2e-shim] real claude is not installed 1>&2\n"
            "exit /b 1\n"
        )
    shim = (
        "@echo off\n"
        f'if "%~1"=="{STUB_FLAG}" (\n'
        f'  "{sys.executable}" -X utf8 "{stub_py}"\n'
        "  exit /b %ERRORLEVEL%\n"
        ")\n"
        f"{real_branch}"
    )
    # Text-mode write translates \n -> os.linesep, so the .cmd lands with
    # proper CRLF line endings on Windows.
    (shim_dir / "claude.cmd").write_text(shim, encoding="ascii")

