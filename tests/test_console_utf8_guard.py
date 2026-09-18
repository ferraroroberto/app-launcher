"""Console entry points that print non-ASCII must reconfigure stdout (#1005).

Global CLAUDE.md, "Recurring gotchas": *Windows Python: UTF-8 stdout under
capture — durable fix: ``sys.stdout.reconfigure(encoding="utf-8")`` at entry
points.* On Windows a piped or captured stdout defaults to cp1252, which
cannot encode the emoji and em-dashes these scripts print, so the process
dies with ``UnicodeEncodeError`` the moment its output is not a console.

Two live consequences motivated this:

* ``scripts/gen_token.py`` printed its confirmation **after**
  ``save_webapp_config`` had already written the token, so the operator got
  a traceback and no confirmation while the config really had changed;
* ``scripts/gen_tailscale_cert.py`` is run with ``capture_output=True`` by
  ``app/webapp/manager.py::check_tailscale_cert``, so the failure surfaced
  as "tailscale cert check failed (ignored)" — blaming the check rather
  than an encoding bug, while the certificate silently went unrenewed.

The check is computed from the AST rather than from a hand-kept list, so a
new script cannot be forgotten and a script that stops printing non-ASCII
stops being required to carry the guard. That also keeps byte-verbatim
vendored scripts out of it automatically: ``classify_e2e.py`` prints only
ASCII, so it is never implicated and must never be edited here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _prints_non_ascii(source: str) -> bool:
    """True when some ``print(...)`` call carries a non-ASCII character.

    Only the call's own source is inspected, so a non-ASCII comment or
    docstring — which never reaches stdout — does not require the guard.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            if not ast.unparse(node).isascii():
                return True
    return False


def _has_guard(source: str) -> bool:
    return 'sys.stdout.reconfigure(encoding="utf-8"' in source


def test_scripts_printing_non_ascii_reconfigure_stdout():
    offenders = []
    checked = 0
    for path in sorted(_SCRIPTS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not _prints_non_ascii(source):
            continue
        checked += 1
        if not _has_guard(source):
            offenders.append(path.name)
    assert checked, "no script prints non-ASCII -- has the scan broken?"
    assert not offenders, (
        "script(s) printing non-ASCII without the stdout reconfigure guard, "
        "so they raise UnicodeEncodeError whenever stdout is piped or "
        "captured on Windows: " + ", ".join(offenders)
    )
