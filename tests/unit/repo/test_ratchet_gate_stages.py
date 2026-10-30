"""The gate's docs-, structure-, instruction-, doc-refs-, dead-code- and conventions-ratchet stages fail on exit 3 and on a new finding, by name.

The real stage text runs against a throwaway repo with `Fail` stubbed to exit 1; a bare
`& $python scripts/<ratchet>.py` would let exit 3 ("not confirmed") pass."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
GATE_PS1 = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
POWERSHELL = (pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
              / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the gate is a PowerShell script")


# A ratchet that refuses to run without its own keys (conventions needs `rules`).
_EXTRA_KEYS = {"conventions": 'rules = ["logging-not-print"]\n'}


def _stage(name: str) -> str:
    phase = name.replace("_", " ")
    head, *tail = name.split("_")
    exit_var = head + "".join(word.capitalize() for word in tail) + "Exit"
    lines = GATE_PS1.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if f'Phase "{phase} ratchet..."' in line)
    end = max(i for i, line in enumerate(lines) if f"${exit_var}" in line)
    return "\n".join(lines[start:end + 1])


@pytest.mark.parametrize("name, files, code, expected", [
    ("docs", {"README.md": "# T\n\n```\nnever closed\n"}, 1, ["docs ratchet NOT CONFIRMED ("]),
    ("docs", {"README.md": "# T\n\nSee #123 for why.\n"}, 1, ["docs ratchet failed", "README.md", "#123"]),
    ("docs", {"README.md": "# T\n\nPlain prose.\n"}, 0, ["STAGE-PASSED"]),
    ("structure", {"src/a.py": "x = 1\n", ".fleet.toml": 'shape = "nope"\n'}, 1,
     ["structure ratchet NOT CONFIRMED ("]),
    ("structure", {"src/big.py": "x = 1\n" * 1100}, 1, ["structure ratchet failed", "src/big.py: 1,100 lines"]),
    ("structure", {"src/a.py": "x = 1\n"}, 0, ["STAGE-PASSED"]),
    ("instruction", {"CLAUDE.md": "# T\n\n```\nnever closed\n"}, 1,
     ["instruction ratchet NOT CONFIRMED ("]),
    ("instruction", {"CLAUDE.md": "# T\n\nAlways double-check your work.\n"}, 1,
     ["instruction ratchet failed", "CLAUDE.md", "R-03"]),
    ("instruction", {"CLAUDE.md": "# T\n\nPlain prose.\n"}, 0, ["STAGE-PASSED"]),
    ("doc_refs", {"README.md": "# T\n\n```\nnever closed\n"}, 1, ["doc refs ratchet NOT CONFIRMED ("]),
    ("doc_refs", {"README.md": "# T\n\nRun `scripts/missing.py` first.\n"}, 1,
     ["doc refs ratchet failed", "README.md", "scripts/missing.py"]),
    ("doc_refs", {"README.md": "# T\n\nPlain prose.\n"}, 0, ["STAGE-PASSED"]),
    ("dead_code", {"src/a.py": "def broken(:\n"}, 1, ["dead code ratchet NOT CONFIRMED ("]),
    ("dead_code", {"src/a.py": "def never_called():\n    pass\n"}, 1,
     ["dead code ratchet failed", "src/a.py", "never_called"]),
    ("dead_code", {"src/a.py": "def used():\n    pass\n\n\nused()\n"}, 0, ["STAGE-PASSED"]),
    ("conventions", {"src/a.py": "def broken(:\n"}, 1, ["conventions ratchet NOT CONFIRMED ("]),
    ("conventions", {"src/a.py": "print('hi')\n"}, 1,
     ["conventions ratchet failed", "src/a.py", "[logging-not-print]"]),
    ("conventions", {"src/a.py": "import logging\n\nlogging.info('hi')\n"}, 0, ["STAGE-PASSED"]),
])
def test_stage_verdict(tmp_path, name: str, files: dict[str, str], code: int, expected: list[str]) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / f"{name}_ratchet.py", tmp_path / "scripts")
    if name == "structure":
        shutil.copy2(REPO_ROOT / "scripts" / "structure_layout.py", tmp_path / "scripts")
    files = {f".{name.replace('_', '-')}-ratchet-baseline.json": '{"version": 1, "entries": []}\n', **files}
    files[".fleet.toml"] = files.get(".fleet.toml", "") + f"[{name}_ratchet]\nenabled = true\n{_EXTRA_KEYS.get(name, '')}"
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(exist_ok=True)
        (tmp_path / path).write_text(text, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "."]):
        subprocess.run(["git", *argv], cwd=tmp_path, check=True, creationflags=_NO_WINDOW)
    harness = tmp_path / "stage.ps1"
    harness.write_text(
        "function Phase($m) {}\nfunction Fail($m) { Write-Host \"FAIL: $m\"; exit 1 }\n"
        f"$python = '{sys.executable}'\nSet-Location '{tmp_path}'\n"
        f"{_stage(name)}\nWrite-Host 'STAGE-PASSED'\n", encoding="utf-8")
    run = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=120)

    assert run.returncode == code, f"stdout={run.stdout!r} stderr={run.stderr!r}"
    for text in expected:
        assert text in run.stdout, run.stdout
    assert ("STAGE-PASSED" in run.stdout) == (code == 0), run.stdout
