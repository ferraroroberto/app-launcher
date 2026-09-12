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

The declaration stays the runtime source of truth (a sha-to-sha diff must be
scoped by what was declared, not by the working tree's imports as they look
today), but it is no longer hand-maintained on trust:
:func:`session_host_import_closure` walks the session-host's real first-party
import graph so a test can hold the declaration to it (#923 — `src/audit.py`
was imported by the session-host and never declared, so a change to it could
not make `stale_relevant` say `true`).
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import List, Optional, Set

from src.git_utils import run_git

logger = logging.getLogger(__name__)

_PATH_TOKEN_RE = re.compile(r"`([^`]+)`")
_SCOPED_BULLET_PREFIXES = ("- what/why:", "- not restarted/deployed by:")

#: The module the ``:8446`` process *is*. ``launcher.py session-host`` is a
#: thin argparse wrapper that imports it, so rooting the closure here keeps it
#: to the session-host's own code; the shared CLI bootstrap above it is loaded
#: identically by the tray and webapp processes and is deliberately out of
#: scope (see ``CLAUDE.md``'s ``## session-host`` "path list" bullet).
SESSION_HOST_ENTRY_MODULE = "app.session_host.server"


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


def session_host_import_closure(
    repo_root: Path, entry_module: str = SESSION_HOST_ENTRY_MODULE
) -> Optional[List[str]]:
    """Repo-relative posix paths of every first-party module the session-host
    process loads, transitively, from :data:`SESSION_HOST_ENTRY_MODULE`.

    Static: the import graph is read with :mod:`ast`, nothing is executed (the
    session-host imports ``winpty``/``pyte`` and binds a port at import time on
    Windows only — importing it to introspect it is not an option). Imports
    inside functions and under ``if TYPE_CHECKING`` are followed too: a module
    named anywhere in the file is one a change can reach, and over-declaring is
    the safe direction.

    Package ``__init__.py`` files on the way to a module count — importing
    ``src.agents`` executes ``src/__init__.py``, so a change there is equally
    live-in-that-process.

    ``None`` — never a partial list — when the entry module can't be resolved
    or any file in the walk can't be read or parsed. A truncated closure would
    silently under-declare, which is the exact failure this function exists to
    catch; "couldn't compute" has to stay its own state (#923).
    """
    entry = _module_file(repo_root, entry_module)
    if entry is None:
        logger.warning(
            "⚠️ session-host import closure: entry module %s not found under %s",
            entry_module, repo_root,
        )
        return None

    seen: Set[str] = set()
    files: Set[str] = set()
    pending = [entry_module]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_file(repo_root, module)
        if path is None:
            continue  # third-party or stdlib — not ours to declare
        files.add(path.relative_to(repo_root).as_posix())
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as exc:
            logger.warning(
                "⚠️ session-host import closure: %s unreadable/unparseable: %s", module, exc
            )
            return None
        pending.extend(_ancestor_packages(module))
        pending.extend(_imported_modules(tree, module, path))
    return sorted(files)


def _module_file(repo_root: Path, module: str) -> Optional[Path]:
    """``module``'s file inside ``repo_root`` (``foo/bar.py`` or
    ``foo/bar/__init__.py``), or ``None`` when it isn't a module of this repo."""
    parts = module.split(".")
    if not all(parts):
        return None
    candidate = repo_root.joinpath(*parts).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package = repo_root.joinpath(*parts, "__init__.py")
    return package if package.is_file() else None


def _ancestor_packages(module: str) -> List[str]:
    parts = module.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def _imported_modules(tree: ast.AST, module: str, path: Path) -> List[str]:
    """Dotted module names imported anywhere in ``tree``, with relative
    imports resolved against ``module``'s own package."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    out: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_from_import(node, package)
            if not target:
                continue
            out.append(target)
            # ``from src.notify import telegram`` — the name may be a
            # submodule rather than an attribute; _module_file sorts it out.
            out.extend(f"{target}.{alias.name}" for alias in node.names)
    return out


def _resolve_from_import(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".") if package else []
    trimmed = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    return ".".join([*trimmed, *([node.module] if node.module else [])])


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
