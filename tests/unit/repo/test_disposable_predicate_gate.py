"""One definition of "this is the disposable e2e/verify instance".

``src.instance_role.is_disposable`` owns the rule. A site that re-reads
``LAUNCHER_SESSION_HOST_PORT`` inline grows its own copy of it, and the copies
drift: the rule is what keeps the gate's throwaway webapp from opening windows
on, or sweeping, the real desktop.
"""

import re
from pathlib import Path

import pytest

from src import instance_role
from src.webapp_config import SESSION_HOST_PORT_ENV

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# webapp_config reads the variable as a port number, a different question.
ALLOWED = {
    PROJECT_ROOT / "src" / "instance_role.py",
    PROJECT_ROOT / "src" / "webapp_config.py",
}
INLINE_READ = re.compile(r"environ\.get\(\s*SESSION_HOST_PORT_ENV")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("   ", False), ("18446", True)],
)
def test_is_disposable_follows_the_port_override(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
    else:
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, value)
    assert instance_role.is_disposable() is expected


def test_no_site_rederives_the_disposable_test_inline():
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}:{number}"
        for root in ("app", "src")
        for path in (PROJECT_ROOT / root).rglob("*.py")
        if path not in ALLOWED
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if INLINE_READ.search(line)
    ]
    assert not offenders, (
        "read the override through instance_role.is_disposable(), not inline: "
        + ", ".join(offenders)
    )
