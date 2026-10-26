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
from app.webapp.routers.apps import (
    _LAUNCH_MODEL_CATALOG,
    _NO_MODEL_FLAG_BUILDERS,
)

# Resolved directly with `model_override` rather than through the table.
# Read off the registry, not re-listed here (#1044): a hand-written copy in
# this file would be a third list of the same fact, free to rot away from
# `src/agents.py` exactly as the two tables in the router could.
_MODEL_CARRYING = {
    name for name, spec in agents.AGENTS.items() if spec.per_launch_model
}


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


def test_the_catalog_covers_every_model_carrying_agent():
    """#1044 — the route indexes `_LAUNCH_MODEL_CATALOG[agent]` directly.

    It may do so only because this holds: the registry flag decides which
    agents reach that line, so an agent marked `per_launch_model` with no
    catalog entry is a `KeyError` (a 500 on the phone) rather than a check
    that quietly does not run.
    """
    missing = sorted(_MODEL_CARRYING - set(_LAUNCH_MODEL_CATALOG))
    assert not missing, (
        "these agents are marked per_launch_model but have no catalog, so "
        f"the launch route would raise KeyError on a model request: {missing}"
    )


def test_the_catalog_names_no_agent_that_cannot_use_it():
    """The other direction: a catalog entry for an agent the route refuses
    before it ever looks one up is dead weight that reads like a contract."""
    unreachable = sorted(set(_LAUNCH_MODEL_CATALOG) - _MODEL_CARRYING)
    assert not unreachable, (
        "these agents are not per_launch_model, so the route 400s before "
        f"reaching the catalog and these entries can never be read: {unreachable}"
    )


def test_the_two_tables_partition_the_registry():
    """Every agent lands on exactly one side of the split.

    `per_launch_model` is the single source of truth; the router's two
    tables are its projections. A seventh agent that reaches neither is a
    launch that cannot build flags at all.
    """
    covered = _MODEL_CARRYING | set(_NO_MODEL_FLAG_BUILDERS)
    assert covered == set(agents.AGENTS), (
        "registry and launch tables disagree; symmetric difference: "
        f"{sorted(covered ^ set(agents.AGENTS))}"
    )
