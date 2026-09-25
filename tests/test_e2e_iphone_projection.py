"""An e2e test that reads engine- or viewport-shaped facts keeps WebKit (#1220).

Since #1220 a browser test runs on the WebKit/iPhone projection only when it
carries the ``iphone`` marker (``tests/e2e/conftest.py``'s
``pytest_collection_modifyitems`` deselects every other WebKit node). The
marker is opt-in, so the way it rots is a test that measures layout, taps or
branches on the engine and forgets it: that test silently stops running on the
iPhone projection, the one place its assertion means anything.

This guard closes that gap for the mechanical half of the rule. It parses each
e2e module with :mod:`ast` (no Playwright needed) and, for every test, follows
the module-level helpers and fixtures it uses. If that source reads geometry
or computed style, taps, uses the viewport or safe-area, or looks at
``browser_name``, the test must carry ``iphone``. The other half, whole
surfaces kept by decision (nav, the composer and keyboard input, smoke), is
marked by hand and needs no guard: over-marking only costs time.

A test that runs only on Chromium anyway (``browser_name != "chromium"`` skips
it everywhere else) is exempt: its WebKit node never ran.

Lives outside ``tests/e2e/`` so it runs in the non-browser suite, like
``test_e2e_smoke_markers.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Set

_E2E_DIR = Path(__file__).resolve().parent / "e2e"

# Reads whose answer depends on the engine or the iPhone viewport.
_SIGNAL = re.compile(
    r"bounding_box|boundingBox|getBoundingClientRect|stable_read|getComputedStyle"
    r"|to_have_css|scroll(Width|Height|Top)|client(Width|Height)"
    r"|offset(Width|Height|Top|Left)|viewport_size|inner(Width|Height)"
    r"|visualViewport|safe-area|elementFromPoint|matchMedia|browser_name"
    r"|\.tap\(|has_touch|is_mobile|maxTouchPoints|ontouch|userAgent"
)
_DESKTOP_ONLY = re.compile(r"""browser_name\s*!=\s*["']chromium["']""")


def _is_iphone_mark(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "iphone"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    )


def _module_marked(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            marks = node.value.elts if isinstance(node.value, ast.List) else [node.value]
            if any(_is_iphone_mark(m) for m in marks):
                return True
    return False


def unmarked_signal_tests(path: Path) -> Dict[str, Set[str]]:
    """``{test name: signals}`` for tests in ``path`` that need ``iphone``."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    if _module_marked(tree):
        return {}
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def reachable(name: str, seen: Set[str]) -> str:
        if name in seen or name not in funcs:
            return ""
        seen.add(name)
        fn = funcs[name]
        text = ast.get_source_segment(source, fn) or ""
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        used |= {a.arg for a in fn.args.args}  # module-level fixtures by name
        return text + "".join(reachable(u, seen) for u in sorted(used))

    missing: Dict[str, Set[str]] = {}
    for name, fn in funcs.items():
        if not name.startswith("test_"):
            continue
        if any(_is_iphone_mark(d) for d in fn.decorator_list):
            continue
        text = reachable(name, set())
        if _DESKTOP_ONLY.search(text):
            continue
        signals = {m.group(0) for m in _SIGNAL.finditer(text)}
        if signals:
            missing[name] = signals
    return missing


def test_engine_or_viewport_tests_keep_the_iphone_projection() -> None:
    modules = sorted(_E2E_DIR.glob("test_*.py"))
    assert modules, "no e2e test modules found -- wrong path?"
    problems = [
        f"{path.name}::{name} ({', '.join(sorted(signals))})"
        for path in modules
        for name, signals in sorted(unmarked_signal_tests(path).items())
    ]
    assert not problems, (
        "these e2e tests read engine- or viewport-shaped facts but would run on "
        "Chromium only; add @pytest.mark.iphone (or the module's pytestmark) "
        "so they keep the WebKit/iPhone projection (#1220):\n  "
        + "\n  ".join(problems)
    )


def test_guard_sees_a_marked_module_and_a_desktop_only_test(tmp_path: Path) -> None:
    # The guard's own branches, pinned so a regex edit can't hollow it out.
    marked = tmp_path / "test_marked.py"
    marked.write_text(
        "import pytest\npytestmark = [pytest.mark.smoke, pytest.mark.iphone]\n"
        "def test_a(page):\n    page.locator('x').bounding_box()\n",
        encoding="utf-8",
    )
    assert unmarked_signal_tests(marked) == {}

    mixed = tmp_path / "test_mixed.py"
    mixed.write_text(
        "import pytest\npytestmark = pytest.mark.smoke\n"
        "def _measure(page):\n    return page.locator('x').bounding_box()\n"
        "def test_helper(page):\n    _measure(page)\n"
        "@pytest.mark.iphone\ndef test_decorated(page):\n    _measure(page)\n"
        "def test_desktop(page, browser_name):\n"
        "    if browser_name != 'chromium':\n        pytest.skip('desktop')\n"
        "    _measure(page)\n"
        "def test_plain(page):\n    page.goto('/')\n",
        encoding="utf-8",
    )
    assert set(unmarked_signal_tests(mixed)) == {"test_helper"}
