"""The perf review's "ready" element is a real card on the landing view."""

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


def test_the_selector_names_the_project_list_of_the_default_tab() -> None:
    list_id = re.match(r"#(\w+) ", _selector())
    assert list_id, _selector()
    pane = (STATIC / "views" / "coding-pane.html").read_text(encoding="utf-8")
    assert f'id="{list_id.group(1)}"' in pane
    tabs = (STATIC / "tabs.js").read_text(encoding="utf-8")
    assert "defaultTab: 'claude'" in tabs and 'id="paneClaude"' in pane


def test_the_project_list_is_filled_with_action_rows() -> None:
    assert _selector().endswith(".action-row")
    coding = (STATIC / "apps" / "apps-coding.js").read_text(encoding="utf-8")
    assert "claudeList" in coding and "actionRow(" in coding
