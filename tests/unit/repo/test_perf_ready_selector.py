"""The perf review's "ready" element is a real element on the landing view."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "app" / "webapp" / "static"


def _selector() -> str:
    data = tomllib.loads((ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    return data["perf"]["review"]["ready_selector"]


def test_a_ready_selector_is_declared() -> None:
    # Without one, "ready" is first contentful paint, which goes green on a
    # shell that has no data yet.
    assert _selector().strip()


def test_the_selector_is_an_id_in_the_default_tab_outside_any_collapsed_section() -> None:
    match = re.fullmatch(r"#(\w+)", _selector())
    assert match, _selector()
    pane = (STATIC / "views" / "coding-pane.html").read_text(encoding="utf-8")
    assert pane.count(f'id="{match.group(1)}"') == 1
    tabs = (STATIC / "tabs.js").read_text(encoding="utf-8")
    assert "defaultTab: 'claude'" in tabs and 'id="paneClaude"' in pane
    # A <details> that is closed on load hides everything inside it; the
    # element must come before the first one.
    before = pane[: pane.index(f'id="{match.group(1)}"')]
    assert before.count("<details") == before.count("</details>")


def test_the_selector_element_is_filled_by_the_sessions_render() -> None:
    match = re.fullmatch(r"#(\w+)", _selector())
    assert match
    head = (STATIC / "home-head.js").read_text(encoding="utf-8")
    assert f"els.{match.group(1)}" in head
