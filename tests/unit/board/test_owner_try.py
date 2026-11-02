"""An old owner check's Try, quoted from the issue's own checklist or said to be absent."""

from __future__ import annotations

import json

import pytest

from src.board import owner_checks, owner_try
from src.clients import github as github_client

GENERIC = (
    "Use what this issue shipped once, as a user, the way the issue describes it; "
    "then answer on the Board: works, or what needs fixing."
)

BODY = """## What & why
Something.

## Acceptance criteria
- [ ] Every open check shows a specific Try
- [x] New checks are unaffected
* A third line
1. A fourth line is dropped

## Out of scope
- not this
"""


def test_the_backfill_boilerplate_is_generic_and_a_written_try_is_not():
    assert owner_try.is_generic(GENERIC)
    assert owner_try.is_generic("  use what this issue shipped once,\nas a user")
    assert not owner_try.is_generic("lock the phone and tap play")
    assert not owner_try.is_generic("")


def test_derive_quotes_the_first_three_checklist_lines_verbatim():
    assert owner_try.derive(BODY) == (
        "Acceptance criteria",
        ["Every open check shows a specific Try", "New checks are unaffected", "A third line"],
    )


@pytest.mark.parametrize("body, expected", [
    ("### Test plan\n- run it\n", ("Test plan", ["run it"])),
    ("**How to verify**\n- open the page\n", ("How to verify", ["open the page"])),
    ("## Test plan\n- t\n## Acceptance criteria\n- a\n", ("Acceptance criteria", ["a"])),
    ("## Acceptance criteria\n```\n- not a line\n```\n- real\n", ("Acceptance criteria", ["real"])),
])
def test_derive_finds_each_known_section_and_prefers_acceptance_criteria(body, expected):
    assert owner_try.derive(body) == expected


@pytest.mark.parametrize("body", [
    "", "no sections here\n- a bullet", "## Acceptance criteria\njust prose\n", "## Other\n- a\n",
])
def test_derive_says_nothing_when_the_issue_has_nothing_to_quote(body):
    assert owner_try.derive(body) is None


def test_a_long_line_is_cut_not_dropped():
    lines = owner_try.derive("## Test plan\n- " + "x" * 500)[1]
    assert len(lines[0]) == owner_try.LINE_MAX and lines[0].endswith("…")


# --- the Board cache -------------------------------------------------------

def _old(number: int) -> dict:
    return {"repo": "demo", "number": number, "ref": f"demo#{number}", "title": "t", "url": "",
            "state": "CLOSED", "kind": "validate-after", "block": True, "reason": "r",
            "try": GENERIC, "proof": [], "since": ""}


@pytest.fixture
def listing(monkeypatch):
    items = [_old(1), _old(2), dict(_old(3), **{"try": "tap play"})]
    monkeypatch.setattr(
        owner_checks, "_run_cli", lambda *_a: (0, json.dumps({"items": items, "unknown": []}))
    )


def _by_ref(snap: dict) -> dict:
    return {it["ref"]: it for it in snap["items"]}


def test_a_generic_try_never_reaches_the_board_and_a_written_one_is_untouched(listing, monkeypatch):
    monkeypatch.setattr(owner_checks, "_fetch_body", lambda *_a: BODY)
    items = _by_ref(owner_checks.refresh("fleet", "octo"))
    assert items["demo#1"]["try"] == "" and items["demo#1"]["try_generic"] is True
    assert items["demo#1"]["try_from"] == "Acceptance criteria"
    assert items["demo#1"]["try_lines"][0] == "Every open check shows a specific Try"
    assert items["demo#3"]["try"] == "tap play" and items["demo#3"]["try_lines"] == []


def test_an_issue_with_no_checklist_shows_no_lines_and_is_not_read_again(listing, monkeypatch):
    reads: list = []
    monkeypatch.setattr(owner_checks, "_fetch_body", lambda *a: reads.append(a) or "just prose")
    first = _by_ref(owner_checks.refresh("fleet", "octo"))
    assert first["demo#1"]["try"] == "" and first["demo#1"]["try_lines"] == []
    owner_checks.refresh("fleet", "octo")
    assert [r[2] for r in reads] == [1, 2]


def test_a_body_that_did_not_read_is_retried_not_remembered_as_none(listing, monkeypatch):
    calls: list = []

    def flaky(_owner, _repo, number):
        calls.append(number)
        if len(calls) <= 2:
            raise github_client.GhError("rate limited")
        return BODY

    monkeypatch.setattr(owner_checks, "_fetch_body", flaky)
    assert _by_ref(owner_checks.refresh("fleet", "octo"))["demo#1"]["try_lines"] == []
    assert _by_ref(owner_checks.refresh("fleet", "octo"))["demo#1"]["try_lines"] != []


def test_no_owner_means_no_body_reads(listing, monkeypatch):
    monkeypatch.setattr(owner_checks, "_fetch_body", lambda *_a: pytest.fail("read a body"))
    assert _by_ref(owner_checks.refresh("fleet"))["demo#1"]["try_lines"] == []
