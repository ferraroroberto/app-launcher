"""Regression pins for the gate progress log's failure excerpts (issue #943).

Each test runs a hermetic inner pytest with ``tests._progress_log`` loaded and
LAUNCHER_VERIFY_PROGRESS_LOG pointed at a file under ``tmp_path`` — never the
real ``webapp/verify-progress.log``. Every credential below is an obvious fake.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests import _credential_hygiene as hygiene
from tests import _progress_log as progress

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_inner_pytest(test_dir: Path, source: str, *plugins: str) -> str:
    """Run ``source`` as a test file under ``plugins``; return the progress log."""
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "test_inner.py").write_text(textwrap.dedent(source), encoding="utf-8")
    log = test_dir / "progress.log"
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONUTF8": "1",
        # Hermetic: only the plugins named with -p load.
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        progress.PROGRESS_ENV: str(log),
    }
    args = [arg for plugin in plugins for arg in ("-p", plugin)]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args, "test_inner.py"],
        cwd=test_dir,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert "failed" in proc.stdout or "error" in proc.stdout, proc.stdout + proc.stderr
    return log.read_text(encoding="utf-8")


def _excerpt_after(log: str, marker: str) -> list[str]:
    """The excerpt lines directly under the first log line containing ``marker``."""
    lines = log.splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    out = []
    for line in lines[start + 1:]:
        if not line.startswith(progress.EXCERPT_PREFIX):
            break
        out.append(line[len(progress.EXCERPT_PREFIX):])
    return out


_FAILING_TESTS = '''
    import pytest

    def _check_totals(got):
        assert got == 3, "totals-mismatch-marker-943"

    def test_call_fails():
        _check_totals(2)

    @pytest.fixture
    def broken_fixture():
        raise RuntimeError("fixture-broke-marker-943")

    def test_setup_fails(broken_fixture):
        pass

    def test_passes():
        pass
'''


def test_failure_leaves_crash_message_and_traceback_in_progress_log(tmp_path: Path) -> None:
    log = _run_inner_pytest(tmp_path, _FAILING_TESTS, "tests._progress_log")

    call = "\n".join(_excerpt_after(log, "FAILED (call) test_inner.py::test_call_fails"))
    assert "totals-mismatch-marker-943" in call  # crash message
    assert "_check_totals" in call  # the frame that raised, not only the test
    assert "test_inner.py:" in call  # file:line location

    setup = "\n".join(_excerpt_after(log, "FAILED (setup) test_inner.py::test_setup_fails"))
    assert "fixture-broke-marker-943" in setup

    # A passing test gets no excerpt, and the one-line-per-event log survives
    # filtering the excerpts back out.
    assert "test_passes" in log
    events = [ln for ln in log.splitlines() if not ln.startswith(progress.EXCERPT_PREFIX)]
    assert all(ln.startswith("[") for ln in events), events


def test_excerpts_are_bounded_per_failure_and_per_run(tmp_path: Path) -> None:
    failures = progress.EXCERPT_MAX_FAILURES + 20
    source = f'''
        import pytest

        @pytest.mark.parametrize("n", range({failures}))
        def test_noisy(n):
            # 500 long lines of failure text per test.
            raise AssertionError("\\n".join("x" * 1000 for _ in range(500)))
    '''
    log = _run_inner_pytest(tmp_path, source, "tests._progress_log")

    assert log.count("FAILED (call)") == failures  # every red is still named
    excerpts = [ln for ln in log.splitlines() if ln.startswith(progress.EXCERPT_PREFIX)]
    # Per failure: the kept tail, one "omitted" marker; plus one cap notice.
    per_failure = progress.EXCERPT_MAX_LINES + 1
    assert len(excerpts) == progress.EXCERPT_MAX_FAILURES * per_failure + 1
    assert "excerpts stop after" in excerpts[-1]
    longest = len(progress.EXCERPT_PREFIX) + progress.EXCERPT_MAX_LINE_CHARS + len(" ...")
    assert max(len(ln) for ln in excerpts) <= longest
    # 50 × 500 KB of failure text lands as well under a megabyte.
    assert len(log.encode("utf-8")) < 500_000


def test_progress_log_excerpt_carries_no_credential(tmp_path: Path) -> None:
    """A credential in a failing assertion must not reach the progress log: the
    excerpt is read after `_credential_hygiene` has scrubbed the report."""
    credential = "fake-live-credential-in-a-progress-log-943"
    source = f'''
        def test_authenticated_request_fails():
            headers = {{"Authorization": "Bearer {credential}"}}
            assert headers is None, f"request headers: {{headers}}"
    '''

    # Control: without the hygiene hook the credential really does reach the
    # log, so the treated run below is proving a redaction, not an absence.
    control = _run_inner_pytest(tmp_path / "control", source, "tests._progress_log")
    assert credential in control, "leak shape no longer reproduces — re-derive this pin"

    # Progress plugin registered *first*: redaction must not depend on order.
    scrubbed = _run_inner_pytest(
        tmp_path / "treated", source, "tests._progress_log", "tests._credential_hygiene"
    )
    assert "FAILED (call)" in scrubbed
    assert credential not in scrubbed
    assert "Bearer " + hygiene.REDACTED in scrubbed
