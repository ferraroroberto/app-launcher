"""Launch-flag dispatch covers every agent, and advertises no dead path (#1006).

`POST /api/apps/{id}/launch` resolves flags two ways: Claude and Codex are
called directly with `model_override`, every other agent goes through
`_NO_MODEL_FLAG_BUILDERS`. The table used to also list `claude` and `codex`,
which no request could ever reach through it - two entries that read like a
dispatch path and were not one.

The tests below pin both halves of that contract, because the cleanup only
holds if the table cannot drift back: an agent added to `agents.AGENTS` with
no builder is a `KeyError` on the first launch from the phone, and a
model-carrying agent added to the table is a silently ignored `--model`.
"""

from __future__ import annotations

from src import agents
from app.webapp.routers.apps import _NO_MODEL_FLAG_BUILDERS

# Resolved directly with `model_override` rather than through the table.
_MODEL_CARRYING = {"claude", "codex"}


def test_every_agent_can_have_its_launch_flags_built():
    missing = sorted(
        name
        for name in agents.AGENTS
        if name not in _MODEL_CARRYING and name not in _NO_MODEL_FLAG_BUILDERS
    )
    assert not missing, (
        "these agents would raise KeyError on launch - add a flag builder, "
        f"or special-case them like Claude/Codex: {missing}"
    )


def test_the_table_advertises_no_unreachable_entry():
    unreachable = sorted(_MODEL_CARRYING & set(_NO_MODEL_FLAG_BUILDERS))
    assert not unreachable, (
        "model-carrying agents are called directly with model_override, so an "
        f"entry here can never be reached and its --model would be dropped: {unreachable}"
    )


def test_the_table_names_only_real_agents():
    unknown = sorted(set(_NO_MODEL_FLAG_BUILDERS) - set(agents.AGENTS))
    assert not unknown, f"table names agents that do not exist: {unknown}"


def test_every_builder_is_callable_with_config_alone():
    """The table's calling convention: `builder(cfg)`, no model argument."""
    import inspect

    for name, builder in sorted(_NO_MODEL_FLAG_BUILDERS.items()):
        params = list(inspect.signature(builder).parameters.values())
        required = [
            p
            for p in params
            if p.default is inspect.Parameter.empty
            and p.kind
            in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        assert len(required) == 1, (
            f"{name}: the launch route calls builder(cfg) with one argument, "
            f"but this builder requires {len(required)}"
        )
