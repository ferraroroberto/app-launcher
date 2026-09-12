"""The e2e real-agent launch target must resolve from any checkout (issue #932).

A merge-verification run works from a fresh *detached* checkout of the merged
default branch, which sits under a scratch root (``E:\tmp\al-merge-<date>``)
and carries none of this repo's gitignored runtime files. The PTY fixtures
launch "this repo" as a Coding-tab row, and that row's id used to be the
hardcoded literal ``app-launcher`` — a name that exists next to the *primary*
checkout and nowhere else. In a scratch checkout the launch 404'd with
``unknown app app-launcher`` and every real-agent test **skipped**, while the
gate printed the same green as a run that covered them.

The fix is derived, not a committed registry:
:func:`tests.e2e.conftest.pin_launch_target` pins the scan root to the launch
target's parent, so its row always exists. No credential is involved, so the
ban on copying a live ``config/webapp_config.json`` into a scratch tree (#907 /
PR #911) is untouched — which :func:`test_pinning_adds_no_credential_key`
asserts directly.

Resolving the row exposed a *second*, independent gate no registry can close:
the agent's own per-directory folder-trust prompt, which a scratch path has
never cleared. So the target is the first *agent-trusted* checkout of this
repository — this one, then the main checkout a linked worktree hangs off —
and with none, the real-agent tests skip naming trust instead of timing out
against a prompt that will never go away. An *unknown* trust state is never
mistaken for a trusted one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.registry import live_claude_code_entries
from src.scanner import slugify
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import CREDENTIAL_KEYS

from tests._credential_hygiene import disposable_webapp_config
from tests.e2e import conftest as e2e_conftest


def _resolved_ids(cfg: dict) -> list:
    return [
        entry.id
        for entry in live_claude_code_entries(
            Path(cfg["projects_dir"]), list(cfg.get("projects_ignore") or [])
        )
    ]


def _scratch_checkout(tmp_path: Path) -> Path:
    """The merge-verification layout: a checkout under an unrelated scratch root."""
    root = tmp_path / "scratch-root"
    (root / "some-other-probe").mkdir(parents=True)
    checkout = root / "al-merge-20260912"
    checkout.mkdir()
    return checkout


def test_launch_target_resolves_without_a_runtime_config(tmp_path: Path) -> None:
    """The fresh-checkout case: no config on disk, no `app-launcher` sibling."""
    target = _scratch_checkout(tmp_path)
    missing = tmp_path / "never-written" / "webapp_config.json"
    cfg = e2e_conftest.pin_launch_target(
        disposable_webapp_config(missing, "e2e-disposable-probe-token"), target
    )

    assert slugify(target.name) in _resolved_ids(cfg), (
        f"launch target {target.name!r} is not a coding row under "
        f"{cfg['projects_dir']} — the real-agent e2e tests would 404 (issue #932)"
    )


def test_launch_target_survives_an_ignore_pattern_hiding_it(tmp_path: Path) -> None:
    """An inherited ``projects_ignore`` must not hide the launch target.

    A real config may legitimately hide checkouts from the Coding tab;
    inheriting that into the disposable config would silently take the launch
    target away again.
    """
    target = _scratch_checkout(tmp_path)
    real = tmp_path / "webapp_config.json"
    real.write_text(
        json.dumps({"projects_ignore": ["al-merge-*", "unrelated-*"]}), encoding="utf-8"
    )
    cfg = e2e_conftest.pin_launch_target(
        disposable_webapp_config(real, "e2e-disposable-probe-token"), target
    )

    assert "al-merge-*" not in cfg["projects_ignore"]
    assert "unrelated-*" in cfg["projects_ignore"], "unrelated patterns must survive"
    assert slugify(target.name) in _resolved_ids(cfg)


def test_pinning_adds_no_credential_key(tmp_path: Path) -> None:
    """#907's ban stands: pinning writes paths, never a secret."""
    before = disposable_webapp_config(Path("does-not-exist.json"), "tok-placeholder")
    after = e2e_conftest.pin_launch_target(dict(before), _scratch_checkout(tmp_path))

    added = set(after) - set(before)
    assert not (added & set(CREDENTIAL_KEYS)), (
        f"pin_launch_target introduced a credential key: {sorted(added)}"
    )
    assert added <= {"projects_dir", "projects_ignore"}


# ------------------------------------------------- which checkout launches


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )


def _repo_with_worktree(tmp_path: Path) -> tuple:
    main = tmp_path / "app-launcher"
    main.mkdir()
    _git(main, "init", "-q")
    # A throwaway repo: isolate it from the user's global hooks (this host's
    # hooksPath rejects any commit author not on its allowlist).
    no_hooks = tmp_path / "no-hooks"
    no_hooks.mkdir()
    _git(main, "-c", f"core.hooksPath={no_hooks}", "-c", "user.name=t",
         "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / "app-launcher-wt-1"
    _git(main, "worktree", "add", "-q", "--detach", str(worktree))
    return main, worktree


def test_a_linked_worktree_falls_back_to_its_main_checkout(tmp_path: Path) -> None:
    main, worktree = _repo_with_worktree(tmp_path)

    checkouts = e2e_conftest.repository_checkouts(worktree)
    assert [p.resolve() for p in checkouts] == [worktree.resolve(), main.resolve()]
    assert [p.resolve() for p in e2e_conftest.repository_checkouts(main)] == [
        main.resolve()
    ]


def test_a_fresh_clone_is_its_own_only_candidate(tmp_path: Path) -> None:
    main, _ = _repo_with_worktree(tmp_path)
    clone = tmp_path / "al-merge-20260912"
    _git(tmp_path, "clone", "-q", str(main), str(clone))

    assert [p.resolve() for p in e2e_conftest.repository_checkouts(clone)] == [
        clone.resolve()
    ]


@pytest.mark.parametrize(
    "trust, expected",
    [
        ({"wt": True, "main": True}, "wt"),      # this checkout wins when trusted
        ({"wt": False, "main": True}, "main"),   # worktree gate keeps its coverage
        ({"wt": None, "main": True}, "main"),    # unknown is not trusted
        ({"wt": False, "main": None}, None),     # nothing established → skip
    ],
)
def test_first_trusted_checkout_is_the_launch_target(
    monkeypatch: pytest.MonkeyPatch, trust: dict, expected
) -> None:
    dirs = {"wt": Path("E:/x/app-launcher-wt-1"), "main": Path("E:/x/app-launcher")}
    monkeypatch.setattr(
        e2e_conftest, "repository_checkouts", lambda _root: [dirs["wt"], dirs["main"]]
    )
    monkeypatch.setattr(
        e2e_conftest,
        "agent_trusts_dir",
        lambda d: trust["wt" if d == dirs["wt"] else "main"],
    )

    target, _probed = e2e_conftest._launch_target_resolution.__wrapped__()
    assert target == (dirs[expected] if expected else None)


# ------------------------------------------------------- agent folder trust


def _state_file(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_trust_is_read_across_separator_and_case_differences(
    tmp_path: Path, monkeypatch
) -> None:
    """The agent records forward slashes; ``_REPO_ROOT`` is a Windows path."""
    recorded = str(Path("E:/some/checkout")).replace("\\", "/")
    monkeypatch.setattr(
        e2e_conftest,
        "_CLAUDE_STATE_FILE",
        _state_file(tmp_path, {"projects": {recorded: {"hasTrustDialogAccepted": True}}}),
    )

    assert e2e_conftest.agent_trusts_dir(Path(r"E:\Some\Checkout")) is True


def test_untrusted_and_unrecorded_dirs_are_both_false(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        e2e_conftest,
        "_CLAUDE_STATE_FILE",
        _state_file(
            tmp_path, {"projects": {"E:/known": {"hasTrustDialogAccepted": False}}}
        ),
    )

    assert e2e_conftest.agent_trusts_dir(Path("E:/known")) is False
    assert e2e_conftest.agent_trusts_dir(Path("E:/never-opened")) is False


def test_unreadable_trust_state_is_unknown_not_trusted(
    tmp_path: Path, monkeypatch
) -> None:
    """An unknown must stay its own state — never folded into either answer."""
    missing = tmp_path / "absent" / ".claude.json"
    monkeypatch.setattr(e2e_conftest, "_CLAUDE_STATE_FILE", missing)
    assert e2e_conftest.agent_trusts_dir(Path("E:/anything")) is None

    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(e2e_conftest, "_CLAUDE_STATE_FILE", garbage)
    assert e2e_conftest.agent_trusts_dir(Path("E:/anything")) is None

    shapeless = _state_file(tmp_path, {"projects": ["not", "a", "map"]})
    monkeypatch.setattr(e2e_conftest, "_CLAUDE_STATE_FILE", shapeless)
    assert e2e_conftest.agent_trusts_dir(Path("E:/anything")) is None
