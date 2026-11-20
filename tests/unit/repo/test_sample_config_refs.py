"""Static gate: a committed sample config or the architecture diagram names only scripts that exist.

The doc-refs ratchet reads Markdown; a `_comment_*` string in `config/*.sample.json`
and a node label in `docs/architecture.mmd` are read by a person following the
setup step, and neither is scanned. A script deleted in favour of `python -m
src.<module>` left both pointing at nothing.
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_REF = re.compile(r"(?<![\w/])scripts/([\w./-]+\.(?:py|ps1|bat))")


def _prose_files():
    yield from sorted((PROJECT_ROOT / "config").glob("*.sample.json"))
    yield PROJECT_ROOT / "docs" / "architecture.mmd"


def test_sample_config_and_diagram_name_only_existing_scripts():
    files = list(_prose_files())
    assert len(files) > 1, "no sample configs found; the glob is broken"
    missing = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}: scripts/{match.group(1)}"
        for path in files
        for match in SCRIPT_REF.finditer(path.read_text(encoding="utf-8"))
        if not (PROJECT_ROOT / "scripts" / match.group(1)).is_file()
    ]
    assert not missing, "name a script that exists, or its `python -m` replacement:\n" + "\n".join(missing)
