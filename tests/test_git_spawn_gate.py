"""Static gate: the ``git`` wrapper's own spawns carry ``env=`` (#1111).

:func:`src.git_utils.git_env` is where ``GIT_OPTIONAL_LOCKS=0`` lives -- the
fix for the stranded 0-byte ``.git/index.lock`` that froze 23 repos at one
timestamp on 2026-09-20 and nine repos for fifteen days before that
(ferraroroberto/fleet-config#939, #667).

A hand-rolled ``git`` spawn outside ``src/git_utils.py`` is held by the
conventions ratchet's ``git-wrapper`` rule (``.fleet.toml``
``[conventions_ratchet]``, baseline zero), which the gate runs over the whole
tree. What the rule cannot see is the wrapper itself: ``run_git`` builds its argv
into a variable and is declared a ``git_wrappers`` file, so nothing checks that
its spawns still pass an ``env=``. Without it the process inherits the webapp's
environment, ``GIT_OPTIONAL_LOCKS`` is unset and the optional index lock comes
back. The behavioural proof lives in ``tests/test_git_utils.py``; this is the
cheap structural half.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPAWN_ATTRS = {"run", "Popen", "call", "check_output", "check_call"}


def test_the_wrapper_still_passes_an_env_to_its_spawn():
    wrapper = PROJECT_ROOT / "src" / "git_utils.py"
    assert wrapper.is_file(), "the git wrapper moved; update this test and `git_wrappers` in .fleet.toml"

    spawns = [
        node
        for node in ast.walk(ast.parse(wrapper.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SPAWN_ATTRS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert spawns, "src/git_utils.py no longer spawns anything -- the exemption is stale"
    for call in spawns:
        kwargs = {kw.arg for kw in call.keywords}
        assert "env" in kwargs, (
            f"src/git_utils.py:{call.lineno} spawns without env= -- GIT_OPTIONAL_LOCKS=0 "
            "would not reach git (#1111)"
        )
