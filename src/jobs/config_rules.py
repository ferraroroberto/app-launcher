"""Cross-field job rules shared by ``job_from_dict`` and ``update_job``.

Each rule takes plain values, not a ``Job``, so the load path (a raw JSON row)
and the edit path (an effective post-edit state) call the same check and the
two write paths cannot drift. Re-exported by :mod:`src.jobs.config_models` and
:mod:`src.jobs.config`.
"""

from __future__ import annotations

from typing import Any


def validate_principal_shape(visible: bool, session_less: bool) -> None:
    """Raise ``ValueError`` if ``visible`` and ``session_less`` are both set.

    An S4U ("run whether logged on or not") task runs in session 0 with no
    interactive desktop, so ``visible``'s whole point — a console window the
    user can watch on the PC — has nowhere to render. Combining them is not
    a smaller version of either behaviour, it is a silently futile config:
    the job would run logged-out *and* show nothing when it ran logged-in.

    Enforced here rather than resolved by precedence because which one the
    owner wants is a real trade, not a default (issue #757). Shared by
    :func:`job_from_dict` and :func:`src.jobs.config.update_job` so the two
    write paths can't drift.
    """
    if visible and session_less:
        raise ValueError(
            "visible and session_less are mutually exclusive: a session-less "
            "(S4U) task has no desktop to show a console window on"
        )


def validate_rerun_safe(raw: Any) -> bool:
    """Return ``raw`` as the ``rerun_safe`` flag; only a real boolean passes.

    ``None`` means "not supplied" (the create route forwards every optional
    field, absent ones as ``None``) and reads as the safe default ``False``.
    Anything else that is not a ``bool`` — ``"true"``, ``1``, ``"yes"`` — is
    refused rather than coerced, because the fleet chief re-runs a job
    unattended on exactly this flag and a truthy string must not grant it.
    """
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ValueError(
            f"rerun_safe must be true or false, got {type(raw).__name__}"
        )
    return raw


def validate_rerun_safe_shape(
    rerun_safe: bool, confirm: bool, elevated: bool
) -> None:
    """Raise ``ValueError`` if ``rerun_safe`` is set alongside ``confirm`` or
    ``elevated``.

    A job that needs a confirm tap, or that cannot be run manually at all,
    is by definition not safe to re-run unattended. Shared by
    :func:`job_from_dict` and :func:`src.jobs.config.update_job` so the two
    write paths can't drift.
    """
    if not rerun_safe:
        return
    if confirm:
        raise ValueError(
            "rerun_safe cannot be combined with confirm: a job that needs a "
            "confirm tap is not safe to re-run unattended"
        )
    if elevated:
        raise ValueError(
            "rerun_safe cannot be combined with elevated: an elevated job "
            "cannot be run manually, so it cannot be re-run unattended"
        )
