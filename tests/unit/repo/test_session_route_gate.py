"""One builder for the ``/api/claude-code/sessions/<sid>`` route prefix.

``sessionRoute`` in ``static/api.js`` owns it. A page module that hand-builds
the prefix grows its own copy, and the copies drift on how the id is encoded.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent.parent.parent / "app" / "webapp" / "static"
OWNER = STATIC / "api.js"
# A string literal that opens the prefix; prose in comments mentions it without a quote.
HAND_BUILT = re.compile(r"""['"`]/api/claude-code/sessions/['"`$]""")


def test_no_module_hand_builds_the_session_route_prefix():
    offenders = [
        f"{path.relative_to(STATIC)}:{number}"
        for path in STATIC.rglob("*.js")
        if path != OWNER and "vendor" not in path.relative_to(STATIC).parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if HAND_BUILT.search(line)
    ]
    assert not offenders, "build session URLs with sessionRoute() from api.js: " + ", ".join(offenders)
