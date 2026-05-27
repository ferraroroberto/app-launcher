"""Compose argv + env from a job's typed parameter values (issue #67).

A job's ``params`` declares the typed inputs; this module turns a
``{name: value}`` dict into ``(argv_tail, env_overlay)``. It is the
single source of truth for parameterised runs — both the webapp router
and the CLI executor reuse it so server-side validation matches
executor-side composition exactly.

The function is pure: no I/O, no globals, no subprocess. The router
calls it up front so bad values become 400 before any run directory is
written; the executor calls it again after un-marshalling the ``--params``
JSON blob so a scheduled invocation goes through the same validator.

Composition rules (also enforced by the editor / dialog client-side):

* Iterate ``job.params`` in declaration order. Positional params and
  flagged params interleave in that order — the user controls argv
  layout by ordering the list.
* ``kind: bool`` with ``flag``: emit ``[flag]`` when truthy, omit when
  falsy. ``param_from_dict`` already rejects bool params without a flag
  or env mapping.
* ``env``-mapped params land in the env overlay, never argv.
* Missing required → ``ValueError``. Unknown value key → ``ValueError``.
  Type mismatch → ``ValueError``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from src.jobs_config import Job, Param

_INT_RE = re.compile(r"^-?\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _coerce(param: Param, raw_value: Any) -> Any:
    """Type-check ``raw_value`` against ``param.kind`` and return it.

    Strings carrying digits (``"42"``) are accepted for ``kind: int`` to
    play nicely with HTML inputs and URL queries; everything else must
    match the declared Python type exactly. Raises ``ValueError`` with a
    user-facing message on mismatch.
    """
    kind = param.kind
    if kind == "string":
        if not isinstance(raw_value, str):
            raise ValueError(
                f"param {param.name!r}: expected string, got {type(raw_value).__name__}"
            )
        return raw_value
    if kind == "int":
        if isinstance(raw_value, bool):
            raise ValueError(
                f"param {param.name!r}: expected int, got bool"
            )
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, str) and _INT_RE.match(raw_value.strip()):
            return int(raw_value.strip())
        raise ValueError(
            f"param {param.name!r}: expected int, got {raw_value!r}"
        )
    if kind == "bool":
        if isinstance(raw_value, bool):
            return raw_value
        # Accept the two common string projections from HTML forms /
        # querystrings — anything else is a typing error worth surfacing.
        if isinstance(raw_value, str) and raw_value.lower() in {"true", "false"}:
            return raw_value.lower() == "true"
        raise ValueError(
            f"param {param.name!r}: expected bool, got {raw_value!r}"
        )
    if kind == "enum":
        if not isinstance(raw_value, str):
            raise ValueError(
                f"param {param.name!r}: expected string, got {type(raw_value).__name__}"
            )
        if raw_value not in (param.options or []):
            raise ValueError(
                f"param {param.name!r}: value {raw_value!r} not in options {param.options!r}"
            )
        return raw_value
    if kind == "date":
        if not isinstance(raw_value, str) or not _DATE_RE.match(raw_value):
            raise ValueError(
                f"param {param.name!r}: expected YYYY-MM-DD, got {raw_value!r}"
            )
        return raw_value
    # Should be unreachable — param_from_dict already constrained kind.
    raise ValueError(f"param {param.name!r}: unsupported kind {kind!r}")


def _resolve_value(param: Param, values: Dict[str, Any]) -> Any:
    """Return the value to use for ``param`` (user-supplied or default).

    Raises ``ValueError`` when a required value is missing.
    """
    if param.name in values:
        return _coerce(param, values[param.name])
    if param.default is not None:
        return param.default
    if param.required:
        raise ValueError(f"param {param.name!r} is required")
    # Non-required, no default → param is skipped entirely (no argv, no env).
    return None


def compose_argv(
    job: Job, values: Dict[str, Any]
) -> Tuple[List[str], Dict[str, str]]:
    """Build ``(argv_tail, env_overlay)`` from ``job.params`` + ``values``.

    The returned ``argv_tail`` is what the executor splices onto the
    interpreter/script invocation *before* the legacy ``job.args``
    whitespace-split tail (so parameter-less jobs keep behaving the same
    as before this feature shipped). The env overlay is meant to be
    merged onto ``os.environ`` by the caller.
    """
    if not isinstance(values, dict):
        raise ValueError(f"params must be an object, got {type(values).__name__}")

    declared = {p.name for p in job.params}
    for key in values:
        if key not in declared:
            raise ValueError(f"unknown param {key!r}")

    argv: List[str] = []
    env: Dict[str, str] = {}
    for param in job.params:
        resolved = _resolve_value(param, values)
        if resolved is None:
            continue
        if param.env:
            env[param.env] = _stringify(param, resolved)
            continue
        if param.kind == "bool":
            # Validated to have a flag (param_from_dict guarantees it).
            if resolved:
                argv.append(param.flag or "")
            continue
        rendered = _stringify(param, resolved)
        if param.flag:
            argv.append(param.flag)
            argv.append(rendered)
        else:
            argv.append(rendered)
    return argv, env


def _stringify(param: Param, value: Any) -> str:
    """Render a coerced value as the string Python's subprocess argv expects."""
    if param.kind == "bool":
        return "true" if value else "false"
    if param.kind == "int":
        return str(int(value))
    return str(value)
