"""The executor's ``--trigger`` contract, at the real argparse boundary (#687).

Every Jobs-tab fire path forwards a trigger string onto the executor's
argv (``src.jobs_schtasks.spawn_run_job_detached``), and the spawn is
fire-and-forget — nothing waits on the child's exit code. So a trigger
value argparse rejects does not surface as an error anywhere: the child
``sys.exit(2)``s before the pid is ever written, and the pre-created
``run.json`` sits at ``status: "pending"`` forever, unreapable by
``src.jobs_reap`` (which needs a pid).

That is exactly what shipped: ``--trigger`` was declared
``choices=["scheduled", "manual"]`` while the code itself constructs
``chain:<upstream_id>`` (DAG ``on_success`` / ``on_failure``, #68) and
``webhook`` (#73). Both features were wired end to end, documented, and
tested — but every fire died in argparse.

The pre-existing chain tests missed it because they mock
``spawn_run_job_detached`` and assert on the string handed to the mock,
so the argparse boundary is never crossed. These tests build the *real*
launcher parser and parse real argv.
"""

from __future__ import annotations

import pytest

from app.cli.main import _build_parser
from src.jobs_trigger import (
    SIMPLE_TRIGGERS,
    chain_trigger,
    chain_upstream_id,
    is_valid_trigger,
)


def _parse(trigger: str):
    return _build_parser().parse_args(
        ["run-job", "some-job", "--run-id", "20260101T000000", "--trigger", trigger]
    )


class TestTriggerArgParsing:
    @pytest.mark.parametrize("trigger", list(SIMPLE_TRIGGERS))
    def test_simple_triggers_parse(self, trigger):
        assert _parse(trigger).trigger == trigger

    def test_chain_trigger_parses(self):
        """The regression pin: `chain:<id>` used to `SystemExit: 2` here."""
        trigger = chain_trigger("upstream-job")
        assert _parse(trigger).trigger == trigger

    def test_webhook_trigger_parses(self):
        """Same defect, second victim: POST /hook fires `trigger="webhook"`."""
        assert _parse("webhook").trigger == "webhook"

    def test_default_is_scheduled(self):
        """Task Scheduler omits the flag entirely."""
        args = _build_parser().parse_args(["run-job", "some-job"])
        assert args.trigger == "scheduled"

    @pytest.mark.parametrize("trigger", ["bogus", "chain:", "chained:up", ""])
    def test_unknown_trigger_still_rejected(self, trigger):
        """Widening the type must not turn `--trigger` into free text."""
        with pytest.raises(SystemExit) as exc:
            _parse(trigger)
        assert exc.value.code == 2


class TestChainTriggerShape:
    def test_round_trip(self):
        assert chain_upstream_id(chain_trigger("up")) == "up"

    def test_non_chain_values_have_no_upstream(self):
        for trigger in SIMPLE_TRIGGERS:
            assert chain_upstream_id(trigger) is None

    def test_bare_prefix_is_not_a_chain_trigger(self):
        assert chain_upstream_id("chain:") is None
        assert not is_valid_trigger("chain:")

    def test_every_minted_trigger_is_accepted(self):
        """Guards the mint sites against drifting out of the vocabulary."""
        for trigger in (*SIMPLE_TRIGGERS, chain_trigger("some-upstream")):
            assert is_valid_trigger(trigger)
