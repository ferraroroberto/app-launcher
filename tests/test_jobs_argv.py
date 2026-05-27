"""``src.jobs_argv.compose_argv`` — typed parameter composition (issue #67).

The argv composer is a pure function: every test wires a hand-built
:class:`~src.jobs_config.Job` with the relevant ``params`` shape, then
exercises composition and validation. No I/O.
"""

from __future__ import annotations

import pytest

from src.jobs_argv import compose_argv
from src.jobs_config import Job, Param


def _mk(*params: Param, args: str = "") -> Job:
    return Job(
        id="demo",
        name="Demo",
        script_path="C:\\stub\\demo.py",
        args=args,
        params=list(params),
    )


class TestParameterless:
    """Jobs without ``params`` must keep behaving exactly as before #67."""

    def test_empty(self):
        argv, env = compose_argv(_mk(), {})
        assert argv == []
        assert env == {}

    def test_rejects_unknown_when_no_params(self):
        with pytest.raises(ValueError, match="unknown param"):
            compose_argv(_mk(), {"x": 1})


class TestPositionalAndFlags:
    def test_positional_in_declaration_order(self):
        job = _mk(
            Param(name="a", kind="string"),
            Param(name="b", kind="string"),
        )
        argv, env = compose_argv(job, {"a": "first", "b": "second"})
        assert argv == ["first", "second"]
        assert env == {}

    def test_flag_pair(self):
        job = _mk(Param(name="since", kind="date", flag="--since"))
        argv, _ = compose_argv(job, {"since": "2026-06-01"})
        assert argv == ["--since", "2026-06-01"]

    def test_interleaved_flags_and_positionals_keep_declaration_order(self):
        job = _mk(
            Param(name="pos1", kind="string"),
            Param(name="since", kind="date", flag="--since"),
            Param(name="pos2", kind="string"),
        )
        argv, _ = compose_argv(
            job, {"pos1": "A", "since": "2026-06-01", "pos2": "B"}
        )
        assert argv == ["A", "--since", "2026-06-01", "B"]


class TestBoolFlag:
    def test_truthy_emits_flag_alone(self):
        job = _mk(Param(name="v", kind="bool", flag="--verbose"))
        argv, _ = compose_argv(job, {"v": True})
        assert argv == ["--verbose"]

    def test_falsy_omits_flag_entirely(self):
        job = _mk(Param(name="v", kind="bool", flag="--verbose"))
        argv, _ = compose_argv(job, {"v": False})
        assert argv == []

    def test_string_true_accepted(self):
        # HTML checkboxes round-trip as "true"/"false" in some shapes.
        job = _mk(Param(name="v", kind="bool", flag="--verbose"))
        argv, _ = compose_argv(job, {"v": "true"})
        assert argv == ["--verbose"]


class TestEnv:
    def test_env_value_lands_in_overlay_not_argv(self):
        job = _mk(Param(name="api", kind="string", env="API_KEY"))
        argv, env = compose_argv(job, {"api": "sekret"})
        assert argv == []
        assert env == {"API_KEY": "sekret"}

    def test_env_bool_serialises_to_true_false(self):
        job = _mk(Param(name="dbg", kind="bool", env="DEBUG"))
        argv, env = compose_argv(job, {"dbg": True})
        assert argv == []
        assert env == {"DEBUG": "true"}


class TestDefaultsAndRequired:
    def test_default_applied_when_missing(self):
        job = _mk(
            Param(
                name="tier", kind="string",
                default="a", required=False, flag="--tier",
            )
        )
        argv, _ = compose_argv(job, {})
        assert argv == ["--tier", "a"]

    def test_missing_required_raises(self):
        job = _mk(Param(name="since", kind="date", flag="--since"))
        with pytest.raises(ValueError, match="is required"):
            compose_argv(job, {})

    def test_optional_no_default_is_skipped(self):
        job = _mk(
            Param(name="since", kind="date", flag="--since", required=False)
        )
        argv, env = compose_argv(job, {})
        assert argv == []
        assert env == {}


class TestTypeChecking:
    def test_int_coerces_string_digits(self):
        job = _mk(Param(name="n", kind="int", flag="--n"))
        argv, _ = compose_argv(job, {"n": "42"})
        assert argv == ["--n", "42"]

    def test_int_rejects_non_numeric(self):
        job = _mk(Param(name="n", kind="int", flag="--n"))
        with pytest.raises(ValueError, match="expected int"):
            compose_argv(job, {"n": "abc"})

    def test_enum_rejects_outside_options(self):
        job = _mk(
            Param(
                name="t", kind="enum", options=["a", "b"], flag="--t",
            )
        )
        with pytest.raises(ValueError, match="not in options"):
            compose_argv(job, {"t": "c"})

    def test_date_format_enforced(self):
        job = _mk(Param(name="d", kind="date", flag="--d"))
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            compose_argv(job, {"d": "06/01/2026"})

    def test_unknown_param_rejected(self):
        job = _mk(Param(name="d", kind="date", flag="--d"))
        with pytest.raises(ValueError, match="unknown param 'x'"):
            compose_argv(job, {"d": "2026-06-01", "x": 1})
