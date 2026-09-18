"""``log_once`` and ``load_fleet_contract`` — the two shared leaf helpers (#1003).

Three sites had hand-rolled the log-once mechanism and two had byte-identical
fleet-config contract loaders. Neither mechanism had direct coverage: the
only existing test (``test_board.py::test_unrecognized_title_glyph_logs_one_
breadcrumb_per_glyph``) pins one caller's once-per-key behaviour and nothing
pinned the caps, the overflow reset, or the loader at all.
"""

from __future__ import annotations

import logging

import pytest

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


def test_distinct_keys_each_get_one_line(caplog):
    seen: set[str] = set()
    with caplog.at_level(logging.INFO, logger=logger.name):
        for key in ("a", "b", "a", "b", "c"):
            log_once(seen, key, 8, logger.info, "key=%s", key)
    assert [r.getMessage() for r in caplog.records] == ["key=a", "key=b", "key=c"]


def test_overflow_clears_the_bucket_wholesale(caplog):
    """The existing behaviour of both capped sites, kept deliberately: these
    are breadcrumbs, not a cache, so a cleared set just means a condition may
    be logged once more rather than growing without bound."""
    seen: set[str] = set()
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_once(seen, "a", 2, logger.info, "x")
        log_once(seen, "b", 2, logger.info, "x")
        assert len(seen) == 2
        # At cap: the set resets, so this both logs and starts a fresh bucket.
        assert log_once(seen, "c", 2, logger.info, "x") is True
    assert seen == {"c"}


def test_emit_receives_the_callers_own_logger(caplog):
    """``emit`` is a bound logger method, so the record is attributed to the
    caller's module, not this helper's."""
    seen: set[str] = set()
    other = logging.getLogger("tests.log_once.elsewhere")
    with caplog.at_level(logging.WARNING, logger=other.name):
        log_once(seen, "k", 4, other.warning, "warned")
    assert [r.name for r in caplog.records] == ["tests.log_once.elsewhere"]


def test_the_three_call_sites_keep_their_own_caps():
    """#1003's finding proposed "one helper with a **shared** cap". The caps
    bound different key spaces and must stay different:

    * 64 glyphs — one per distinct leading glyph, stable for a session's life
    * 512 suppressed rows — one per session id, on a five-second board poll
    * 64 claim failures — previously **uncapped**, which was the real defect

    Collapsing them to one number would be a behaviour change dressed as a
    cleanup, so this fails if someone later "tidies" them into one constant.
    """
    from src.active_issue_claims import _FAILURE_LOG_CAP
    from src.board_sessions import _SUPPRESSED_LOG_CAP
    from src.board_transcript import _UNKNOWN_TITLE_GLYPH_LOG_CAP

    assert _UNKNOWN_TITLE_GLYPH_LOG_CAP == 64
    assert _SUPPRESSED_LOG_CAP == 512
    assert _FAILURE_LOG_CAP == 64
    assert _SUPPRESSED_LOG_CAP != _UNKNOWN_TITLE_GLYPH_LOG_CAP, (
        "the board-rows bucket bounds a far larger, poll-driven key space "
        "than the glyph bucket; one shared cap would shrink or bloat one of "
        "them (#1003)"
    )


def test_claim_failure_warnings_are_now_bounded(caplog):
    """``active_issue_claims._warn_once`` was the one site with no cap at
    all, so a process seeing many distinct failure strings grew the set
    without limit."""
    from src import active_issue_claims as m

    m._logged_failures.clear()
    with caplog.at_level(logging.WARNING, logger="src.active_issue_claims"):
        for i in range(m._FAILURE_LOG_CAP + 5):
            m._warn_once(f"reason-{i}")
    assert len(m._logged_failures) <= m._FAILURE_LOG_CAP
    m._logged_failures.clear()


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
