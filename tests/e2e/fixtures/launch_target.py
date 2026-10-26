"""The checkout a real-agent test launches in, and the config pinned to scan only it."""

from __future__ import annotations

import functools
import glob
import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from src.git_utils import run_git
from src.scanner import dir_ignored

from tests.e2e.fixtures.env import REPO_ROOT

logger = logging.getLogger(__name__)

# Claude Code's per-directory folder-trust gate (issue #932). A directory the
# user has never opened the agent in gets a full-screen "Is this a project you
# created or one you trust?" prompt on first launch, *instead* of the composer
# — so a real-agent assertion can never land there. It is independent of the
# permission mode: `--dangerously-skip-permissions` was measured NOT to clear
# it. The only ways to clear it are answering the prompt or writing
# `hasTrustDialogAccepted` into the user's global `~/.claude.json`, and a test
# harness must do neither — so a real-agent test launches in a checkout that
# already cleared it, or skips naming trust rather than time out against a
# prompt that will never go away.
_CLAUDE_STATE_FILE = Path.home() / ".claude.json"


def agent_trusts_dir(project_dir: Path) -> Optional[bool]:
    """Has the agent's folder-trust gate been accepted for ``project_dir``?

    ``None`` when the answer can't be established at all (no state file, or
    one this harness can't parse) — an unknown, never folded into either
    answer. A directory absent from the map is a definite ``False``: the agent
    records an entry when the gate is accepted.
    """
    try:
        state = json.loads(_CLAUDE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    projects = state.get("projects") if isinstance(state, dict) else None
    if not isinstance(projects, dict):
        return None

    def _key(value: str) -> str:
        return os.path.normcase(os.path.normpath(value))

    want = _key(str(project_dir))
    for recorded, entry in projects.items():
        if _key(str(recorded)) == want:
            return bool(
                isinstance(entry, dict) and entry.get("hasTrustDialogAccepted")
            )
    return False


def repository_checkouts(repo_root: Path) -> List[Path]:
    """``repo_root``, then its repository's main checkout if it is a linked worktree.

    A linked worktree (`app-launcher-wt-N`) has never cleared the agent's
    trust gate, but the main checkout it hangs off usually has — and before
    #932 the literal target id quietly launched there, so worktree gates ran
    the real-agent pin. Keeping that as an explicit, derived fallback stops
    the fix for fresh checkouts from costing worktrees the same coverage. A
    fresh clone *is* its own main checkout, so it gets no fallback.
    """
    checkouts = [repo_root]
    common = run_git(
        repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    )
    if common:
        common_dir = Path(common)
        if common_dir.name == ".git":
            main = common_dir.parent
            if os.path.normcase(str(main.resolve())) != os.path.normcase(
                str(repo_root.resolve())
            ):
                checkouts.append(main)
    return checkouts


_TrustProbe = Tuple[Tuple[Path, Optional[bool]], ...]


@functools.lru_cache(maxsize=None)
def _launch_target_resolution() -> Tuple[Optional[Path], _TrustProbe]:
    """Resolve once per run: (first trusted checkout or ``None``, every probe)."""
    probed = tuple((d, agent_trusts_dir(d)) for d in repository_checkouts(REPO_ROOT))
    target = next((d for d, trusted in probed if trusted is True), None)
    return target, probed


def launch_target_dir() -> Optional[Path]:
    """The checkout a real-agent test launches in, or ``None`` if none is trusted."""
    return _launch_target_resolution()[0]


def pin_launch_target(cfg: dict, target: Path) -> dict:
    """Make ``cfg`` scan ``target`` and nothing else, so its coding row resolves.

    Pins the Coding-tab scan root to ``target``'s parent and drops any
    inherited ignore pattern that would hide ``target`` itself, so the launch
    stops 404ing in a fresh scratch checkout (issue #932). Nothing here is a
    credential — a checkout's own path is not secret — so it does not reopen
    #907 / PR #911.

    Every sibling of ``target`` is ignored (issue #1512). The SPA fetches
    ``/api/claude-code/git-status`` on every page boot, and that runs two or
    three ``git`` processes per scanned project: with the whole projects dir
    (~50 repos on the dev box) each test's boot spawned ~150 of them, the
    disposable webapps spent most of their time in git, and the box sat at
    100% CPU for the whole browser phase until unrelated page loads timed out.
    A test that needs other project rows mocks ``/api/apps`` or git-status.

    Mutates and returns ``cfg``. Pinned by ``tests/unit/repo/test_e2e_launch_target.py``.
    """
    cfg["projects_dir"] = str(target.parent)
    kept = [
        pattern
        for pattern in (cfg.get("projects_ignore") or [])
        if not dir_ignored(target.name, [pattern])
    ]
    try:
        siblings = sorted(
            child.name for child in target.parent.iterdir() if child.name != target.name
        )
    except OSError as exc:
        logger.warning("⚠️ could not list %s, the e2e webapp will scan all of it: %s",
                       target.parent, exc)
        siblings = []
    # glob.escape: a sibling name holding [ ] * ? must match only itself.
    cfg["projects_ignore"] = kept + [glob.escape(name) for name in siblings]
    return cfg


# UI preferences the e2e suite's assertions depend on. The disposable config
# is derived from the checkout's real one (and a new worktree copies the live
# one), so a value the owner picked in Settings would otherwise leak into every
# test that reads the default (#1458: ``usage_providers: ["claude"]`` hides the Codex
# row). A new setting a test asserts against belongs here.
E2E_PINNED_UI_PREFS = {"usage_providers": ["claude", "codex"]}


def pin_ui_prefs(cfg: dict) -> dict:
    """Overwrite ``cfg``'s UI preferences with :data:`E2E_PINNED_UI_PREFS`.

    Mutates and returns the disposable config only; the checkout's real
    ``config/webapp_config.json`` is never written. Pinned by
    ``tests/unit/repo/test_e2e_launch_target.py``.
    """
    cfg.update(E2E_PINNED_UI_PREFS)
    return cfg


def require_trusted_launch_target() -> Path:
    """The real-agent launch target, or a skip that names why there is none."""
    target, probed = _launch_target_resolution()
    if target is not None:
        return target
    states = "; ".join(
        f"{d}: {'untrusted' if trusted is False else 'UNKNOWN'}"
        for d, trusted in probed
    )
    pytest.skip(
        "no checkout of this repository has cleared the agent's folder-trust "
        f"gate ({states}), so the agent would paint its trust prompt instead "
        "of a composer and a real-agent assertion can never land (issue #932). "
        "An unknown state is not assumed trusted. Clearing the gate would mean "
        "writing the user's global agent state, which the gate must not do — "
        "this is the residual coverage a fresh detached checkout costs."
    )
