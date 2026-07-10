"""Chain-cycle graph algorithm for the jobs registry (split off ``src.jobs_config``).

``on_success`` / ``on_failure`` edges between jobs form a DAG dispatch
graph (issue #68). This module owns the graph-shaped checks — cycle
detection and unknown-reference validation — that ``add_job`` /
``update_job`` run before ever writing a chain edit to disk. See
:mod:`src.jobs_config_models` for the ``Job``/``JobsConfig`` dataclasses
these functions read.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from src.jobs_config_models import JobsConfig


def detect_chain_cycle(cfg: JobsConfig) -> Optional[List[str]]:
    """Return a sample cycle (list of job ids ending where it began),
    or ``None`` when the chain graph is acyclic.

    Edges = ``on_success ∪ on_failure``. A job's downstream consequences
    on either branch are equivalent for cycle purposes: if A→B is on
    success and B→A is on failure, A and B still form a cycle.
    """
    graph: Dict[str, List[str]] = {
        j.id: list(dict.fromkeys((j.on_success or []) + (j.on_failure or [])))
        for j in cfg.jobs
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in graph}
    stack: List[str] = []

    def visit(u: str) -> Optional[List[str]]:
        color[u] = GRAY
        stack.append(u)
        for v in graph.get(u, ()):
            if v not in color:
                # Unknown downstream — reference error, surfaced separately.
                continue
            if color[v] == GRAY:
                return stack[stack.index(v):] + [v]
            if color[v] == WHITE:
                sub = visit(v)
                if sub is not None:
                    return sub
        color[u] = BLACK
        stack.pop()
        return None

    for node in graph:
        if color[node] == WHITE:
            sub = visit(node)
            if sub is not None:
                return sub
    return None


def _check_chain_references(cfg: JobsConfig) -> None:
    """Raise ``ValueError`` if any ``on_success`` / ``on_failure`` entry
    points at a job id that does not exist in the registry.

    Catching this at save time stops a typo'd downstream from silently
    being a no-op for years; the user gets the error in the dialog.
    """
    known = {j.id for j in cfg.jobs}
    for j in cfg.jobs:
        for field_name, edges in (
            ("on_success", j.on_success),
            ("on_failure", j.on_failure),
        ):
            for did in edges or ():
                if did == j.id:
                    raise ValueError(
                        f"{j.id}.{field_name}: a job cannot chain to itself"
                    )
                if did not in known:
                    raise ValueError(
                        f"{j.id}.{field_name}: unknown downstream job id "
                        f"{did!r}"
                    )


def _validate_chain_consistency(cfg: JobsConfig) -> None:
    """Combined references + cycle check, called from ``add_job`` /
    ``update_job`` so the on-disk state is always acyclic and complete.
    """
    _check_chain_references(cfg)
    cycle = detect_chain_cycle(cfg)
    if cycle is not None:
        raise ValueError(
            "chain cycle detected: " + " → ".join(cycle)
        )
