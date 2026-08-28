"""Declared session-host paths + touched-path diff, for `/api/version`'s
staleness scoping (#635).

`_session_host_freshness()` in `app/webapp/routers/misc.py` used to flag the
session-host `stale` the instant its loaded `git_sha` differed from the
repo's current `HEAD` — true after *any* merge anywhere in the repo, not just
one that touched code the session-host actually loads. This module supplies
the missing scope: which paths the session-host declares
(`CLAUDE.md`'s `## session-host` block, project-scaffolding's deploy-coverage
convention, `#629`) and whether the diff between two shas touched any of them.

Hand-rolled rather than importing `fleet-config`'s
`skills/_lib/deploy_coverage.py` (which does the equivalent parse/intersect
for the `/issue-finish` skill) — fleet-config is a sibling checkout, not a
Python dependency of this repo, and this module only ever needs one fixed
section (`## session-host`), not deploy_coverage's general multi-component
parser.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from src.git_utils import run_git

_PATH_TOKEN_RE = re.compile(r"`([^`]+)`")
_SCOPED_BULLET_PREFIXES = ("- what/why:", "- not restarted/deployed by:")


def declared_session_host_paths(claude_md_path: Path) -> List[str]:
    """Backtick-quoted path tokens from CLAUDE.md's ``## session-host``
    section (the ``what/why`` and ``not restarted/deployed by`` bullets).

    Empty list when the file, the section, or any parseable path token is
    missing — callers must treat that as "can't scope" (unknown), never as
    "nothing declared, so nothing touched".
    """
    try:
        text = claude_md_path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = text.splitlines()
    in_section = False
    bullets: List[str] = []
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip() == "## session-host"
            continue
        if in_section and line.strip().startswith("-"):
            bullets.append(line.strip())
    tokens: List[str] = []
    for bullet in bullets:
        if bullet.lower().startswith(_SCOPED_BULLET_PREFIXES):
            tokens.extend(_PATH_TOKEN_RE.findall(bullet))
    return [t for t in tokens if "/" in t and " " not in t]


def paths_touched_between(
    repo_root: Path, base_sha: str, head_sha: str, paths: List[str]
) -> Optional[bool]:
    """Whether ``git diff --name-only base_sha..head_sha`` touched any of
    ``paths``.

    ``None`` when the diff itself can't be resolved (an unknown/unreachable
    sha, a shallow clone, git missing from PATH) or ``paths`` is empty — never
    a confident "no" when the comparison couldn't actually run.
    """
    if not paths:
        return None
    out = run_git(
        repo_root,
        ["diff", "--name-only", f"{base_sha}..{head_sha}"],
        warn_on_failure=True,
    )
    if out is None:
        return None
    changed = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return _touched_by(changed, paths)


def _touched_by(changed_files: List[str], path_tokens: List[str]) -> bool:
    norm_changed = [f.replace("\\", "/") for f in changed_files]
    for f in norm_changed:
        for tok in path_tokens:
            tok_n = tok.replace("\\", "/")
            if tok_n.endswith("/"):
                stripped = tok_n.rstrip("/")
                if f == stripped or f.startswith(tok_n):
                    return True
            elif f == tok_n or f.endswith("/" + tok_n):
                return True
    return False
