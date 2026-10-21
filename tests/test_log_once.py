"""``log_once`` and ``load_fleet_contract``, the shared leaf helpers (#1003); the caps are pinned at the call sites."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from src import active_issue_claims, board, board_sessions, board_transcript
from src._fleet_contract import load_fleet_contract
from src._log_once import log_once

logger = logging.getLogger("tests.log_once")


def test_logs_the_first_time_and_stays_quiet_after(caplog):
    seen: set[str] = set()
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert log_once(seen, "k", 8, logger.info, "hello %s", "world") is True
        assert log_once(seen, "k", 8, logger.info, "hello %s", "world") is False
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["hello world"]


def test_emit_receives_the_callers_own_logger(caplog):
    """``emit`` is a bound logger method, so the record is attributed to the
    caller's module, not this helper's."""
    seen: set[str] = set()
    other = logging.getLogger("tests.log_once.elsewhere")
    with caplog.at_level(logging.WARNING, logger=other.name):
        log_once(seen, "k", 4, other.warning, "warned")
    assert [r.name for r in caplog.records] == ["tests.log_once.elsewhere"]


def _glyph_site(_tmp_path, i):
    board_transcript._live_title_is_busy(f"{chr(0x4E00 + i)} chief")


def _suppressed_row_site(_tmp_path, i):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    row = {"project": "x", "status": "working", "cwd": "E:/x/y", "updated_at": stamp}
    board.merge_sessions([], {f"sid-{i:05d}": row})


def _claim_failure_site(tmp_path, i):
    active_issue_claims.classify_claims(
        {"k": {}}, live_session_ids=set(), fleet_config_dir=tmp_path / f"fc{i}"
    )


# The caps bound different key spaces and must stay different (#1003): 64
# glyphs, 512 suppressed session ids polled every five seconds, 64 failure
# reasons. One shared number would shrink or bloat one of them.
@pytest.mark.parametrize(
    ("site", "cap", "needle"),
    [
        pytest.param(_glyph_site, 64, "unrecognized glyph", id="title-glyphs"),
        pytest.param(_suppressed_row_site, 512, "suppressed unverifiable", id="suppressed-rows"),
        pytest.param(_claim_failure_site, 64, "claims unverified", id="claim-failures"),
    ],
)
def test_each_call_site_goes_quiet_for_its_own_cap(
    caplog, monkeypatch, tmp_path, site, cap, needle
):
    for module, name in (
        (board_transcript, "_LOGGED_UNKNOWN_TITLE_GLYPHS"),
        (board_sessions, "_LOGGED_SUPPRESSED_ROWS"),
        (active_issue_claims, "_logged_failures"),
    ):
        monkeypatch.setattr(module, name, set())
    caplog.set_level(logging.INFO)

    def lines() -> int:
        return sum(needle in r.getMessage() for r in caplog.records)

    for i in range(cap):
        site(tmp_path, i)
    assert lines() == cap  # every distinct key logs once
    site(tmp_path, 0)
    assert lines() == cap  # a repeat inside the cap is quiet
    site(tmp_path, cap)
    assert lines() == cap + 1  # one past the cap logs and resets the bucket
    site(tmp_path, 1)
    assert lines() == cap + 2  # so an early key is allowed to log again


# ------------------------------------------------- load_fleet_contract

def test_loads_a_contract_module_from_a_path(tmp_path):
    contract = tmp_path / "contract.py"
    contract.write_text("VALUE = 41\n", encoding="utf-8")
    module = load_fleet_contract(str(contract), "probe_alias_ok", "probe")
    assert module.VALUE == 41
    assert module.__name__ == "probe_alias_ok"


def test_unloadable_contract_names_the_caller_in_the_error(tmp_path):
    """The labelled ``ImportError`` fires when ``spec_from_file_location``
    returns ``None`` — an extension Python has no loader for — so the
    failure still says *which* contract could not be loaded.

    A *missing* ``.py`` is a different path: the spec resolves fine and
    ``exec_module`` raises ``FileNotFoundError``. Pinned below so the two
    are not confused; both copies behaved this way before #1003 and still
    do.
    """
    unloadable = tmp_path / "contract.txt"
    unloadable.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(ImportError) as excinfo:
        load_fleet_contract(str(unloadable), "probe_alias_bad", "quota")
    assert "quota" in str(excinfo.value)


def test_missing_contract_file_raises_from_exec(tmp_path):
    missing = tmp_path / "nope" / "contract.py"
    with pytest.raises(FileNotFoundError):
        load_fleet_contract(str(missing), "probe_alias_missing", "active-issue")


def test_both_callers_share_one_loader():
    """The duplicate is gone and stays gone: neither module may grow its own
    ``spec_from_file_location`` copy again (#1003)."""
    import inspect

    from src import active_issue_claims, quota_usage

    for mod in (active_issue_claims, quota_usage):
        src = inspect.getsource(mod)
        assert "spec_from_file_location" not in src, (
            f"{mod.__name__} re-derived the contract loader instead of "
            "importing load_fleet_contract"
        )
