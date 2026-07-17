"""Scoped API bearer token library — src.api_tokens (issue #72)."""

from __future__ import annotations

from src import api_tokens


def _mint(label="Deck", scope=None):
    return api_tokens.mint_token(label, scope or {"jobs": ["reporting"]})


class TestMint:
    def test_record_shape(self):
        record, raw = _mint()
        assert record["id"].startswith("tok-")
        assert record["label"] == "Deck"
        assert record["scope"] == {"jobs": ["reporting"]}
        assert record["created_at"]
        assert record["last_used_at"] == ""
        # The raw token is never stored — only the salted hash.
        assert raw not in record.values()
        assert len(record["hash"]) == 64
        bytes.fromhex(record["salt"])  # valid hex

    def test_tokens_are_unique(self):
        _, raw1 = _mint()
        _, raw2 = _mint()
        assert raw1 != raw2


class TestFindMatch:
    def test_roundtrip(self):
        record, raw = _mint()
        match = api_tokens.find_match(raw, [record])
        assert match is not None and match.id == record["id"]

    def test_wrong_token_no_match(self):
        record, _raw = _mint()
        assert api_tokens.find_match("not-the-token", [record]) is None

    def test_empty_presented_no_match(self):
        record, _raw = _mint()
        assert api_tokens.find_match("", [record]) is None

    def test_malformed_rows_skipped(self):
        record, raw = _mint()
        rows = [
            "not-a-dict",
            {"id": "tok-x"},  # missing salt/hash
            {"id": "tok-y", "salt": "zz-not-hex", "hash": "aa", "scope": "*"},
            {"id": "tok-z", "salt": "aa", "hash": "bb", "scope": ["bad"]},
            record,
        ]
        match = api_tokens.find_match(raw, rows)
        assert match is not None and match.id == record["id"]


class TestScopeRejection:
    def test_full_scope_allows_everything(self):
        record, _ = _mint(scope="*")
        token = api_tokens.parse_tokens([record])[0]
        assert api_tokens.scope_rejection(token, "GET", "/api/jobs") is None
        assert api_tokens.scope_rejection(token, "POST", "/api/tokens") is None

    def test_scoped_allows_its_run_path(self):
        record, _ = _mint()
        token = api_tokens.parse_tokens([record])[0]
        assert (
            api_tokens.scope_rejection(token, "POST", "/api/jobs/reporting/run")
            is None
        )

    def test_scoped_rejects_other_job(self):
        record, _ = _mint()
        token = api_tokens.parse_tokens([record])[0]
        msg = api_tokens.scope_rejection(token, "POST", "/api/jobs/other/run")
        assert msg is not None and "'other'" in msg

    def test_scoped_rejects_other_endpoints_and_methods(self):
        record, _ = _mint()
        token = api_tokens.parse_tokens([record])[0]
        assert api_tokens.scope_rejection(token, "GET", "/api/jobs") is not None
        assert (
            api_tokens.scope_rejection(token, "GET", "/api/jobs/reporting/run")
            is not None
        )
        assert api_tokens.scope_rejection(token, "GET", "/api/tokens") is not None


class TestTouchLastUsed:
    def test_stamps_matching_row(self):
        record, _ = _mint()
        assert api_tokens.touch_last_used([record], record["id"]) is True
        assert record["last_used_at"] != ""

    def test_unknown_id_false(self):
        record, _ = _mint()
        assert api_tokens.touch_last_used([record], "tok-unknown") is False

    def test_public_dict_hides_secret_material(self):
        record, _ = _mint()
        public = api_tokens.parse_tokens([record])[0].public_dict()
        assert "hash" not in public and "salt" not in public
