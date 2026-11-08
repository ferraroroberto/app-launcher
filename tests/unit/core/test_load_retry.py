"""Pins for the gate's one load retry (tests/_load_retry.py).

The failure texts are the shapes the gate recorded when the box stalled under
other lanes' load, with the ports and paths invented.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import _load_retry as retry

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@pytest.mark.parametrize("text", [
    "playwright._impl._errors.TimeoutError: Page.goto: Timeout 15000ms exceeded.",
    "playwright._impl._errors.TimeoutError: Locator.click: Timeout 15000ms exceeded.",
    "requests.exceptions.ReadTimeout: HTTPConnectionPool(host='127.0.0.1', port=50000): Read timed out.",
    "TimeoutError: timed out",
    "playwright._impl._errors.Error: Page.goto: net::ERR_NO_BUFFER_SPACE at https://127.0.0.1:50000/",
    "Page.goto: net::ERR_ADDRESS_IN_USE at https://127.0.0.1:50000/",
    "AssertionError: Locator expected to have count '1'\nActual value: 0\nCall log:\n"
    "  - Expect \"to_have_count\" locator(\"#documentBody\").locator(\".md-diagram svg\") with timeout 5000ms",
])
def test_timeouts_and_socket_errors_are_load_shaped(text: str) -> None:
    assert retry.is_load_shaped(text)


@pytest.mark.parametrize("text", [
    "AssertionError: ('', 'rgb(217, 119, 87)')",
    "AssertionError: two taps on #tabBoard never reached it; events seen: ['pointerdown:use']",
    "Page.goto: net::ERR_CONNECTION_REFUSED at https://127.0.0.1:50000/",
    "[gw1] node down: worker crashed",
])
def test_a_wrong_value_or_a_dead_server_is_not(text: str) -> None:
    assert not retry.is_load_shaped(text)


def _write(path: Path, *rows: dict) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_plan_retries_each_load_failure_once(tmp_path: Path) -> None:
    f = _write(tmp_path / "f.jsonl",
               {"nodeid": "t.py::a[chromium]", "when": "call", "load": True},
               {"nodeid": "t.py::b[chromium-x, y]", "when": "setup", "load": True},
               {"nodeid": "t.py::a[chromium]", "when": "teardown", "load": True})
    assert retry.plan(f) == (0, ["RETRY=t.py::a[chromium]", "RETRY=t.py::b[chromium-x, y]"])


def test_plan_refuses_when_any_failure_is_not_load_shaped(tmp_path: Path) -> None:
    f = _write(tmp_path / "f.jsonl",
               {"nodeid": "t.py::a", "when": "call", "load": True},
               {"nodeid": "t.py::b", "when": "call", "load": False})
    code, lines = retry.plan(f)
    assert code == 1 and lines == ["NO-RETRY=not a timeout or socket error: t.py::b"]


def test_plan_refuses_more_failures_than_a_stall_makes(tmp_path: Path) -> None:
    rows = [{"nodeid": f"t.py::n{i}", "when": "call", "load": True} for i in range(retry.MAX_RETRY + 1)]
    assert retry.plan(_write(tmp_path / "f.jsonl", *rows))[0] == 1


def test_a_missing_or_empty_file_is_not_established(tmp_path: Path) -> None:
    assert retry.plan(tmp_path / "absent.jsonl")[0] == 3
    assert retry.plan(_write(tmp_path / "empty.jsonl"))[0] == 3


def test_the_plugin_records_a_failed_report(tmp_path: Path) -> None:
    (tmp_path / "test_inner.py").write_text(textwrap.dedent("""
        def test_stalls():
            raise TimeoutError("timed out")

        def test_wrong():
            assert 1 == 2

        def test_fine():
            pass
    """), encoding="utf-8")
    failures = tmp_path / "failures.jsonl"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST_")},
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        retry.FAILURES_ENV: str(failures),
    }
    subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "tests._load_retry", "test_inner.py"],
        cwd=tmp_path, env=env, capture_output=True, timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    rows = [json.loads(ln) for ln in failures.read_text(encoding="utf-8").splitlines()]
    assert [(r["nodeid"], r["load"]) for r in rows] == [
        ("test_inner.py::test_stalls", True), ("test_inner.py::test_wrong", False),
    ]
