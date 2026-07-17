"""Jobs registry dataclasses + validation (split off ``src.jobs_config``).

``Schedule`` / ``Param`` / ``Job`` are the shape of one row in
``config/jobs.json``, plus every ``*_from_dict`` / ``validate_*`` helper
that turns raw JSON into a validated dataclass (or raises ``ValueError``
on malformed input). ``JobsConfig`` is the whole-file container.

This module owns *shape*, not *storage* or *graph* concerns — see
:mod:`src.jobs_config` (load/save/CRUD, the registry facade) and
:mod:`src.jobs_config_chain` (the chain-cycle graph algorithm). Import
from ``src.jobs_config`` in new code; it re-exports this module's public
surface for backward compatibility, same pattern as the earlier
``src.jobs`` facade split (issue #315).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from re import compile as _re_compile
from typing import Any, Dict, List, Optional, Union

from src.jobs_webhook import WebhookConfig, webhook_from_dict
from src.scanner import slugify

# Bounded set of schedule types. Anything else fails validation.
SCHEDULE_TYPES = frozenset(
    {"none", "minutes", "hourly", "daily", "daily_times", "weekly", "once"}
)

# schtasks accepts MON|TUE|WED|THU|FRI|SAT|SUN (uppercase, three-letter).
WEEKLY_DAYS = frozenset({"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"})

_HHMM_RE = _re_compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# Once-shot schedule input: ISO-style ``YYYY-MM-DDTHH:MM`` (no seconds,
# no timezone). The dialog uses ``<input type="datetime-local">`` which
# emits exactly this format. Validated tighter than the schedule.at
# field because schtasks /SD /ST treats malformed dates as silent
# nothing-scheduled.
_ONCE_AT_RE = _re_compile(
    r"^(\d{4})-(\d{2})-(\d{2})T([01]\d|2[0-3]):([0-5]\d)$"
)

# Bounded set of typed-parameter kinds (issue #67). Anything else fails
# validation. Mirrors the same closed-set discipline as SCHEDULE_TYPES.
PARAM_KINDS = frozenset({"string", "int", "enum", "bool", "date"})

_PARAM_NAME_RE = _re_compile(r"^[a-z][a-z0-9_]*$")
_PARAM_FLAG_RE = _re_compile(r"^--[a-zA-Z][a-zA-Z0-9_-]*$")
_PARAM_ENV_RE = _re_compile(r"^[A-Z][A-Z0-9_]*$")
_PARAM_DATE_RE = _re_compile(r"^\d{4}-\d{2}-\d{2}$")

# Upper bound on cooldown — a full day. Anything past this is almost
# certainly a typo (e.g. ms thought of as seconds) and the rest of the
# stack would render it confusingly.
MAX_COOLDOWN_SECONDS = 86_400

# Mutex group identifier — lowercase, alnum + hyphen/underscore, 1..32
# chars. Same conservative shape as the job id slug; intentionally not a
# free-form string so the UI can show it back as a pill without escaping
# and the queue-file key stays filesystem-safe (even though the queue is
# one file with the group as a JSON key, not a dir name).
MAX_MUTEX_GROUP_LEN = 32
_MUTEX_GROUP_RE = _re_compile(r"^[a-z][a-z0-9_-]{0,31}$")


# ---------------------------------------------------------------- Schedule


@dataclass
class Schedule:
    """A job's trigger cadence — see module docstring for the bounded set."""

    type: str = "none"
    every: Optional[int] = None
    at: Union[str, List[str], None] = None
    day: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type}
        if self.every is not None:
            payload["every"] = self.every
        if self.at is not None:
            payload["at"] = self.at
        if self.day is not None:
            payload["day"] = self.day
        return payload

    def chip(self) -> str:
        """Compact human label for the UI ('daily 06:00', 'every 5 min', …)."""
        if self.type == "none":
            return ""
        if self.type == "minutes":
            return f"every {self.every} min"
        if self.type == "hourly":
            return f"every {self.every} h"
        if self.type == "daily":
            return f"daily {self.at}"
        if self.type == "daily_times" and isinstance(self.at, list):
            return "daily " + " ".join(self.at)
        if self.type == "weekly":
            return f"{self.day} {self.at}"
        if self.type == "once" and isinstance(self.at, str):
            # "2026-06-01T14:30" → "once 2026-06-01 14:30" (one-off
            # readability beats the ISO-T separator)
            return "once " + self.at.replace("T", " ")
        return self.type


def _validate_schedule(sched: Schedule) -> None:
    """Raise ``ValueError`` if ``sched`` is malformed for its type."""
    if sched.type not in SCHEDULE_TYPES:
        raise ValueError(f"unknown schedule type: {sched.type!r}")
    if sched.type == "none":
        return
    if sched.type in ("minutes", "hourly"):
        if not isinstance(sched.every, int) or sched.every <= 0:
            raise ValueError(
                f"schedule {sched.type!r} requires every > 0, got {sched.every!r}"
            )
        if sched.type == "hourly" and sched.every > 23:
            # schtasks /SC HOURLY /MO accepts 1..23.
            raise ValueError("hourly schedule every must be 1..23")
        return
    if sched.type == "daily":
        if not isinstance(sched.at, str) or not _HHMM_RE.match(sched.at):
            raise ValueError(f"daily schedule needs at=HH:MM, got {sched.at!r}")
        return
    if sched.type == "daily_times":
        if not isinstance(sched.at, list) or not sched.at:
            raise ValueError("daily_times schedule needs a non-empty at list")
        for t in sched.at:
            if not isinstance(t, str) or not _HHMM_RE.match(t):
                raise ValueError(f"daily_times entry must be HH:MM, got {t!r}")
        return
    if sched.type == "weekly":
        if sched.day not in WEEKLY_DAYS:
            raise ValueError(
                f"weekly schedule day must be one of {sorted(WEEKLY_DAYS)}"
            )
        if not isinstance(sched.at, str) or not _HHMM_RE.match(sched.at):
            raise ValueError(f"weekly schedule needs at=HH:MM, got {sched.at!r}")
        return
    if sched.type == "once":
        if not isinstance(sched.at, str) or not _ONCE_AT_RE.match(sched.at):
            raise ValueError(
                f"once schedule needs at=YYYY-MM-DDTHH:MM, got {sched.at!r}"
            )


def schedule_from_dict(raw: Any) -> Schedule:
    """Parse a JSON-shape schedule, raising ``ValueError`` on malformed input."""
    if raw is None:
        return Schedule(type="none")
    if not isinstance(raw, dict):
        raise ValueError(f"schedule must be an object, got {type(raw).__name__}")
    sched = Schedule(
        type=str(raw.get("type") or "none"),
        every=raw.get("every"),
        at=raw.get("at"),
        day=(str(raw["day"]).upper() if raw.get("day") else None),
    )
    _validate_schedule(sched)
    return sched


# ------------------------------------------------------------------ Param


@dataclass
class Param:
    """One typed input declaration for a job (issue #67).

    Used by ``src.jobs_argv.compose_argv`` at run-time to validate user
    input and project it into argv/env. Kind is closed (see
    :data:`PARAM_KINDS`); the editor / run-now dialog renders inputs
    from these declarations.
    """

    name: str
    kind: str
    default: Any = None
    required: bool = True
    options: Optional[List[str]] = None
    flag: Optional[str] = None
    env: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name, "kind": self.kind}
        # Required is the common case; only emit it explicitly when False
        # so historical configs round-trip cleanly.
        if not self.required:
            payload["required"] = False
        if self.default is not None:
            payload["default"] = self.default
        if self.options is not None:
            payload["options"] = list(self.options)
        if self.flag:
            payload["flag"] = self.flag
        if self.env:
            payload["env"] = self.env
        return payload


def _validate_default(name: str, kind: str, default: Any,
                      options: Optional[List[str]]) -> Any:
    """Type-check ``default`` against ``kind`` and return it (coerced)."""
    if kind == "string":
        if not isinstance(default, str):
            raise ValueError(
                f"param {name!r}: default must be a string, got {type(default).__name__}"
            )
        return default
    if kind == "int":
        # bool is a subclass of int — reject explicitly to avoid accidents.
        if isinstance(default, bool) or not isinstance(default, int):
            raise ValueError(
                f"param {name!r}: default must be an int, got {type(default).__name__}"
            )
        return default
    if kind == "bool":
        if not isinstance(default, bool):
            raise ValueError(
                f"param {name!r}: default must be true/false, got {default!r}"
            )
        return default
    if kind == "enum":
        if not isinstance(default, str) or default not in (options or []):
            raise ValueError(
                f"param {name!r}: default {default!r} must be one of {options!r}"
            )
        return default
    if kind == "date":
        if not isinstance(default, str) or not _PARAM_DATE_RE.match(default):
            raise ValueError(
                f"param {name!r}: default must be YYYY-MM-DD, got {default!r}"
            )
        return default
    raise ValueError(f"param {name!r}: unsupported kind {kind!r}")


def param_from_dict(raw: Any) -> Param:
    """Parse + validate one ``Param`` row. Raises ``ValueError`` on bad input.

    Validation is the only place these rules live; ``compose_argv`` trusts
    the resulting :class:`Param` and the router translates ``ValueError``
    into HTTP 400.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"param row must be an object, got {type(raw).__name__}")

    name = str(raw.get("name") or "").strip()
    if not _PARAM_NAME_RE.match(name):
        raise ValueError(
            f"param name {name!r} must be snake_case (start with a letter)"
        )

    kind = str(raw.get("kind") or "").strip()
    if kind not in PARAM_KINDS:
        raise ValueError(
            f"param {name!r}: kind must be one of {sorted(PARAM_KINDS)}, got {kind!r}"
        )

    # options: required for enum, rejected for other kinds.
    raw_options = raw.get("options")
    options: Optional[List[str]] = None
    if kind == "enum":
        if not isinstance(raw_options, list) or not raw_options:
            raise ValueError(
                f"param {name!r}: kind=enum requires a non-empty options list"
            )
        if not all(isinstance(o, str) and o for o in raw_options):
            raise ValueError(
                f"param {name!r}: options must be non-empty strings"
            )
        # Defensive de-dup while preserving order — a duplicate enum slot
        # is almost certainly a user typo and confuses the UI dropdown.
        seen: set = set()
        deduped: List[str] = []
        for o in raw_options:
            if o in seen:
                raise ValueError(
                    f"param {name!r}: duplicate option {o!r}"
                )
            seen.add(o)
            deduped.append(o)
        options = deduped
    elif raw_options not in (None, []):
        raise ValueError(
            f"param {name!r}: options only valid for kind=enum"
        )

    flag = raw.get("flag")
    if flag is not None:
        if not isinstance(flag, str) or not _PARAM_FLAG_RE.match(flag):
            raise ValueError(
                f"param {name!r}: flag {flag!r} must look like --foo"
            )

    env = raw.get("env")
    if env is not None:
        if not isinstance(env, str) or not _PARAM_ENV_RE.match(env):
            raise ValueError(
                f"param {name!r}: env {env!r} must be UPPER_SNAKE_CASE"
            )

    if flag and env:
        raise ValueError(
            f"param {name!r}: flag and env are mutually exclusive"
        )

    # bool without a flag or env has no useful representation — emit-as-
    # positional would produce a literal "true"/"false" argv entry, which
    # is footgun-y and not used anywhere in this repo.
    if kind == "bool" and not flag and not env:
        raise ValueError(
            f"param {name!r}: kind=bool requires either a flag or an env mapping"
        )

    default = raw.get("default")
    if default is not None:
        default = _validate_default(name, kind, default, options)

    # required defaults to True unless a default is present, in which case
    # absence is fine. Explicit "required" in raw wins over the heuristic.
    if "required" in raw:
        required = bool(raw["required"])
    else:
        required = default is None

    return Param(
        name=name,
        kind=kind,
        default=default,
        required=required,
        options=options,
        flag=(flag or None),
        env=(env or None),
    )


def params_from_dict(raw: Any) -> List[Param]:
    """Parse a list of param rows. Empty / missing → ``[]``."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"params must be a list, got {type(raw).__name__}")
    result: List[Param] = []
    names: set = set()
    for row in raw:
        param = param_from_dict(row)
        if param.name in names:
            raise ValueError(f"duplicate param name: {param.name!r}")
        names.add(param.name)
        result.append(param)
    return result


# -------------------------------------------------------------------- Job


@dataclass
class Job:
    id: str
    name: str
    script_path: str
    args: str = ""
    schedule: Schedule = field(default_factory=Schedule)
    added_at: str = ""
    params: List[Param] = field(default_factory=list)
    cooldown_seconds: Optional[int] = None
    mutex_group: Optional[str] = None
    on_success: List[str] = field(default_factory=list)
    on_failure: List[str] = field(default_factory=list)
    # When True, a manual fire must carry explicit confirmation
    # (``?confirmed=1`` on the run route / a confirm dialog in the UI)
    # so a fat-fingered tap or stray Stream Deck press can't execute a
    # destructive job by accident (issue #69).
    confirm: bool = False
    # When True, the job's scheduled Task Scheduler entry runs under
    # ``python.exe`` (a real console window in the logged-on session)
    # instead of the silent ``pythonw.exe``, and the executor tees the
    # child's output to that console as well as ``output.log``. Opt-in:
    # for a job the user wants to *watch* run on the PC (e.g. the weekly
    # fleet codebase-audit) while still capturing output for remote
    # run-history. See ``src.jobs.task_run_command`` and the executor tee.
    visible: bool = False
    # When True, the job's scheduled Task Scheduler entry is created with
    # ``/RL HIGHEST`` (Task Scheduler's silent elevation — no interactive
    # UAC prompt, unlike ``Start-Process``), for a script that needs admin
    # rights to do its work (e.g. restarting an app whose manifest requires
    # elevation). See ``src.jobs_schtasks.sync_schtasks``.
    elevated: bool = False
    # When non-None, ``schedule`` is the placeholder ``Schedule(type="none")``
    # and ``paused_schedule`` carries the *real* shape so resume can
    # restore it untouched. See pause_job/resume_job.
    paused_schedule: Optional[Schedule] = None
    # Job-kind registry (issue #70). Empty ``kind`` is the back-compat
    # default: dispatch infers the kind from ``script_path``'s suffix
    # (``.py``/``.bat``, exactly as before this field existed). An
    # explicit kind (``"powershell"``, ``"shell-wsl"``, ``"inline-shell"``,
    # ``"http-check"``, or explicitly ``"python"``/``"batch"``) opts into
    # the matching module under ``src.jobs_kinds``. ``kind_config`` is a
    # generic settings bag for whatever the active kind needs — inline-shell's
    # ``script_body``/``ext``, http-check's ``url``/``method``/
    # ``expect_status``/``timeout`` — so adding a future synthetic kind never
    # requires touching this dataclass again.
    kind: str = ""
    kind_config: Dict[str, Any] = field(default_factory=dict)
    # Webhook trigger (issue #73): an external service (GitHub, Stripe, a
    # generic POST) can fire this job over POST /api/jobs/<id>/hook, gated
    # by the provider signature instead of the bearer token. See
    # src.jobs_webhook for verification + payload-mapping.
    webhook: Optional[WebhookConfig] = None
    # Per-job env-var overlay (issue #72): {NAME: value} merged into the
    # child's environment by the executor at fire time. Values are either
    # literal strings or "$secret:<key>" references resolved against
    # webapp_config.secrets (src.jobs_secrets) — so jobs.json carries only
    # opaque references, never a real credential. An unresolvable
    # reference finalises the run as failed with a clear note.
    env: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "script_path": self.script_path,
            "args": self.args,
            "schedule": self.schedule.to_dict(),
            "added_at": self.added_at,
        }
        # Only emit params when non-empty so legacy jobs.json rows survive
        # a load → save round-trip without sprouting empty arrays.
        if self.params:
            payload["params"] = [p.to_dict() for p in self.params]
        # cooldown_seconds: omit when unset / zero so legacy rows stay
        # byte-for-byte after a load → save round-trip.
        if self.cooldown_seconds:
            payload["cooldown_seconds"] = self.cooldown_seconds
        if self.mutex_group:
            payload["mutex_group"] = self.mutex_group
        if self.on_success:
            payload["on_success"] = list(self.on_success)
        if self.on_failure:
            payload["on_failure"] = list(self.on_failure)
        if self.confirm:
            payload["confirm"] = True
        if self.visible:
            payload["visible"] = True
        if self.elevated:
            payload["elevated"] = True
        if self.paused_schedule is not None:
            payload["paused_schedule"] = self.paused_schedule.to_dict()
        # kind / kind_config: omit when unset so legacy rows (and every
        # explicit-.py/.bat row saved before this feature shipped) stay
        # byte-for-byte after a load → save round-trip.
        if self.kind:
            payload["kind"] = self.kind
        if self.kind_config:
            payload["kind_config"] = dict(self.kind_config)
        if self.webhook is not None:
            payload["webhook"] = self.webhook.to_dict()
        if self.env:
            payload["env"] = dict(self.env)
        return payload

    @property
    def is_paused(self) -> bool:
        return self.paused_schedule is not None

    @property
    def target_kind(self) -> str:
        """The effective job-kind name (registry name, or ``"unknown"``).

        Explicit ``self.kind`` wins; otherwise inferred from
        ``script_path``'s suffix. See :func:`src.jobs_kinds.resolve_kind`
        for the exact fallback rule — imported locally to avoid a
        ``jobs_config`` ↔ ``jobs_kinds`` import cycle (every kind module
        imports :class:`Job` from here).
        """
        from src.jobs_kinds import resolve_kind  # local import avoids a cycle

        return resolve_kind(self)


def _validate_cooldown(raw: Any) -> Optional[int]:
    """Parse + validate ``cooldown_seconds`` for a job row.

    Accepts ``None`` / missing / explicit ``0`` (all collapse to "no
    cooldown" → ``None``). Otherwise must be an ``int`` in ``[1,
    MAX_COOLDOWN_SECONDS]``. Bool is rejected explicitly because
    ``bool`` is a subclass of ``int`` in Python.
    """
    if raw is None or raw == 0:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            f"cooldown_seconds must be a non-negative int, got "
            f"{type(raw).__name__}"
        )
    if raw < 0:
        raise ValueError(
            f"cooldown_seconds must be >= 0, got {raw}"
        )
    if raw > MAX_COOLDOWN_SECONDS:
        raise ValueError(
            f"cooldown_seconds must be <= {MAX_COOLDOWN_SECONDS}, got {raw}"
        )
    return raw


def _validate_chain_list(field_name: str, raw: Any) -> List[str]:
    """Parse + shape-check an ``on_success`` / ``on_failure`` list.

    Returns a defensively-copied list of strings. Missing / ``None`` /
    empty → ``[]``. Each entry must be a non-empty string; the
    cross-config cycle + reference checks live in
    :func:`src.jobs_config_chain._validate_chain_consistency` because
    they need the full registry to evaluate.
    """
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"{field_name} must be a list, got {type(raw).__name__}"
        )
    out: List[str] = []
    seen: set = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"{field_name}: every entry must be a non-empty job id, "
                f"got {entry!r}"
            )
        ident = entry.strip()
        if ident in seen:
            raise ValueError(f"{field_name}: duplicate entry {ident!r}")
        seen.add(ident)
        out.append(ident)
    return out


def _validate_mutex_group(raw: Any) -> Optional[str]:
    """Parse + validate ``mutex_group``. Empty / missing → ``None``.

    Shape mirrors a slug — lowercase, alnum + ``_`` or ``-``, must start
    with a letter, max 32 chars. Conservative on purpose: the value
    appears verbatim in UI pills and is used as a JSON key in the queue
    file, so we keep it boring and predictable.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"mutex_group must be a string, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        return None
    if not _MUTEX_GROUP_RE.match(stripped):
        raise ValueError(
            f"mutex_group {stripped!r} must be lowercase alnum + _/- "
            f"starting with a letter, up to {MAX_MUTEX_GROUP_LEN} chars"
        )
    return stripped


def env_from_dict(raw: Any) -> Dict[str, str]:
    """Parse + validate a job's ``env`` overlay (issue #72).

    Missing / ``None`` → ``{}``. Keys must be UPPER_SNAKE_CASE (same shape
    as ``Param.env``); values must be strings — either literals or
    ``$secret:<key>`` references (resolution happens at fire time, so a
    reference to a not-yet-created key saves fine and fails the run with a
    clear note instead).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"env must be an object, got {type(raw).__name__}")
    env: Dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _PARAM_ENV_RE.match(name):
            raise ValueError(
                f"env name {name!r} must be UPPER_SNAKE_CASE"
            )
        if not isinstance(value, str):
            raise ValueError(
                f"env {name!r}: value must be a string, got {type(value).__name__}"
            )
        env[name] = value
    return env


def kind_config_from_dict(raw: Any) -> Dict[str, Any]:
    """Parse ``kind_config``. Missing / ``None`` → ``{}``."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"kind_config must be an object, got {type(raw).__name__}")
    return dict(raw)


def validate_kind_shape(
    kind: str, script_path: str, kind_config: Dict[str, Any]
) -> None:
    """Raise ``ValueError`` unless ``(kind, script_path, kind_config)`` is a
    structurally valid combination (issue #70's job-kind registry).

    Shared by :func:`job_from_dict`, :func:`src.jobs_config.update_job`, and
    the webapp's ``PUT /api/jobs/<id>`` route so the three call sites can't
    drift.

    * ``kind == ""`` (legacy / unset) — behaviour is byte-for-byte
      unchanged from before this field existed: ``script_path`` is
      required and must end ``.py`` or ``.bat``. A job opts into a new
      kind by setting ``kind`` explicitly; suffix-inference never expands
      beyond the two suffixes that already worked.
    * ``kind == "inline-shell"`` — no ``script_path``; ``kind_config``
      needs a non-empty ``script_body`` and an ``ext`` in
      ``.ps1``/``.bat``/``.sh``.
    * ``kind == "http-check"`` — no ``script_path``; ``kind_config`` needs
      a non-empty ``url``.
    * Any other explicit kind (``python``/``batch``/``powershell``/
      ``shell-wsl``) — a file-kind, just needs a non-empty ``script_path``;
      the kind's own ``validate()``/``build_argv()`` own the rest.
    """
    from src.jobs_kinds import KINDS  # local import avoids a cycle

    if kind and kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")

    if not kind:
        if not script_path:
            raise ValueError("script_path is required")
        suffix = Path(script_path).suffix.lower()
        if suffix not in (".py", ".bat"):
            raise ValueError(
                f"script_path must end .py or .bat, got {script_path!r}"
            )
        return

    if kind == "inline-shell":
        if script_path:
            raise ValueError("inline-shell jobs must not set script_path")
        if not str(kind_config.get("script_body") or "").strip():
            raise ValueError("inline-shell requires kind_config.script_body")
        if kind_config.get("ext") not in (".ps1", ".bat", ".sh"):
            raise ValueError(
                "inline-shell requires kind_config.ext to be one of "
                ".ps1/.bat/.sh"
            )
        return

    if kind == "http-check":
        if script_path:
            raise ValueError("http-check jobs must not set script_path")
        if not str(kind_config.get("url") or "").strip():
            raise ValueError("http-check requires kind_config.url")
        return

    # An explicit file-kind (python/batch/powershell/shell-wsl).
    if not script_path:
        raise ValueError(f"kind {kind!r} requires script_path")


def job_from_dict(raw: Dict[str, Any]) -> Job:
    """Build a :class:`Job` from one JSON row. Raises on invalid input."""
    kind = str(raw.get("kind") or "").strip()
    script_path = str(raw.get("script_path") or "").strip()
    kind_config = kind_config_from_dict(raw.get("kind_config"))
    validate_kind_shape(kind, script_path, kind_config)
    job = Job(
        id=str(raw.get("id") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        script_path=script_path,
        args=str(raw.get("args") or ""),
        schedule=schedule_from_dict(raw.get("schedule")),
        added_at=str(raw.get("added_at") or ""),
        params=params_from_dict(raw.get("params")),
        cooldown_seconds=_validate_cooldown(raw.get("cooldown_seconds")),
        mutex_group=_validate_mutex_group(raw.get("mutex_group")),
        on_success=_validate_chain_list("on_success", raw.get("on_success")),
        on_failure=_validate_chain_list("on_failure", raw.get("on_failure")),
        confirm=bool(raw.get("confirm", False)),
        visible=bool(raw.get("visible", False)),
        elevated=bool(raw.get("elevated", False)),
        paused_schedule=(
            schedule_from_dict(raw["paused_schedule"])
            if raw.get("paused_schedule") is not None
            else None
        ),
        kind=kind,
        kind_config=kind_config,
        webhook=webhook_from_dict(raw.get("webhook")),
        env=env_from_dict(raw.get("env")),
    )
    if not job.id:
        raise ValueError("job id is required")
    if not job.name:
        raise ValueError("job name is required")
    return job


def make_job_id(name: str, existing_ids: Optional[List[str]] = None) -> str:
    """Slugify ``name`` into a job id, suffixing to avoid collisions."""
    base = slugify(name) or "job"
    if not existing_ids:
        return base
    have = set(existing_ids)
    if base not in have:
        return base
    n = 2
    while f"{base}-{n}" in have:
        n += 1
    return f"{base}-{n}"


# ----------------------------------------------------------- JobsConfig


@dataclass
class JobsConfig:
    jobs: List[Job] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"jobs": [j.to_dict() for j in self.jobs]}
