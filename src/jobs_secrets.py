"""``$secret:<key>`` reference resolution for jobs (issue #72).

One gitignored place for secret *values* — the ``secrets`` block in
``config/webapp_config.json`` — referenced from ``config/jobs.json`` by
opaque ``$secret:<key>`` strings so the jobs registry never carries a
real credential. Two consumers:

* ``Job.env`` (this issue): a per-job env-var overlay whose values may be
  literals or ``$secret:`` references, resolved by the executor at fire
  time and merged into the child's environment.
* ``webhook.secret`` (issue #73, which landed first as the in-line
  analogue of this mechanism): :func:`src.jobs_webhook.resolve_secret`
  delegates here.

Deliberately a leaf module — no imports from the jobs/webapp packages —
so both ``jobs_webhook`` and the executor can use it without cycles.
"""

from __future__ import annotations

import re
from typing import Dict, Mapping

SECRET_REF_RE = re.compile(r"^\$secret:(.+)$")


def resolve_secret_value(value: str, secrets: Mapping[str, str]) -> str:
    """Resolve one string: ``$secret:<key>`` → the stored value, anything
    else → used as-is.

    Raises ``ValueError`` when the referenced key is missing — the caller
    turns that into a failed run / rejected fire with the message intact.
    """
    match = SECRET_REF_RE.match(value)
    if match is None:
        return value
    key = match.group(1)
    if key not in secrets:
        raise ValueError(f"secret {key!r} not found")
    return secrets[key]


def resolve_env_overlay(
    env: Mapping[str, str], secrets: Mapping[str, str]
) -> Dict[str, str]:
    """Resolve a job's ``env`` mapping into literal values.

    Returns a fresh dict safe to merge into a child environment. Raises
    ``ValueError`` on the first unresolvable ``$secret:`` reference.
    """
    return {
        name: resolve_secret_value(str(value), secrets)
        for name, value in env.items()
    }
