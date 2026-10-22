"""The gate's docs-ratchet stage fails on exit 3 and on a new marker, by name.

The real stage text from `scripts/verify-before-ship.ps1` runs against a
throwaway repo with the gate's `Fail` stubbed to exit 1. A bare
`& $python scripts/docs_ratchet.py` would let exit 3 ("not confirmed") pass.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE_PS1 = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = (pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="the gate is a Windows PowerShell script")


def _docs_stage() -> str:
    lines = GATE_PS1.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if 'Phase "docs ratchet..."' in line)
    end = max(i for i, line in enumerate(lines) if "$docsExit" in line)
    return "\n".join(lines[start:end + 1])


@pytest.mark.parametrize("readme, code, expected", [
    ("# T\n\n```\nnever closed\n", 1, ["NOT CONFIRMED"]),
    ("# T\n\nSee #123 for why.\n", 1, ["docs ratchet failed", "README.md", "#123"]),
    ("# T\n\nPlain prose.\n", 0, ["STAGE-PASSED"]),
])
def test_stage_verdict(tmp_path: pathlib.Path, readme: str, code: int, expected: list[str]) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "docs_ratchet.py", tmp_path / "scripts")
    (tmp_path / ".fleet.toml").write_text("[docs_ratchet]\nenabled = true\n", encoding="utf-8")
    (tmp_path / ".docs-ratchet-baseline.json").write_text(
        '{"version": 1, "entries": []}\n', encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "."]):
        subprocess.run(["git", *argv], cwd=tmp_path, check=True, creationflags=_NO_WINDOW)
    harness = tmp_path / "stage.ps1"
    harness.write_text(
        "function Phase($m) {}\n"
        "function Fail($m) { Write-Host \"FAIL: $m\"; exit 1 }\n"
        f"$python = '{sys.executable}'\nSet-Location '{tmp_path}'\n"
        f"{_docs_stage()}\nWrite-Host 'STAGE-PASSED'\n", encoding="utf-8")
    run = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=120)

    assert run.returncode == code, f"stdout={run.stdout!r} stderr={run.stderr!r}"
    for text in expected:
        assert text in run.stdout, run.stdout
    assert ("STAGE-PASSED" in run.stdout) == (code == 0), run.stdout
