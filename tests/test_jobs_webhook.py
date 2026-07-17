"""Webhook-target jobs — verifiers, mapping resolver, secrets (issue #73)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from src.jobs_config import job_from_dict
from src.jobs_webhook import (
    WebhookConfig,
    event_allowed,
    resolve_mapping,
    resolve_secret,
    verify_generic,
    verify_github,
    verify_stripe,
    webhook_from_dict,
)
from src.webapp_config import WebappConfig


def _github_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _stripe_sig(secret: str, body: bytes, t: int) -> str:
    signed = f"{t}.".encode() + body
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


class TestVerifyGithub:
    def test_canonical_vector(self):
        # GitHub's own webhook-docs example — a sanity check against the
        # well-known third-party vector, not just internal self-consistency.
        secret = "It's a Secret to Everybody"
        body = b"Hello, World!"
        header = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
        assert verify_github(secret, body, header) is True

    def test_valid_signature(self):
        body = b'{"ref": "refs/heads/main"}'
        header = _github_sig("s3cr3t", body)
        assert verify_github("s3cr3t", body, header) is True

    def test_wrong_secret(self):
        body = b'{"ref": "refs/heads/main"}'
        header = _github_sig("s3cr3t", body)
        assert verify_github("wrong", body, header) is False

    def test_tampered_body(self):
        header = _github_sig("s3cr3t", b'{"ref": "refs/heads/main"}')
        assert verify_github("s3cr3t", b'{"ref": "refs/heads/evil"}', header) is False

    def test_missing_header(self):
        assert verify_github("s3cr3t", b"{}", None) is False

    def test_malformed_header(self):
        assert verify_github("s3cr3t", b"{}", "not-sha256-prefixed") is False


class TestVerifyStripe:
    def test_valid_signature(self):
        body = b'{"id": "evt_1"}'
        header = _stripe_sig("whsec_test", body, t=1_700_000_000)
        assert verify_stripe(
            "whsec_test", body, header, now=1_700_000_000
        ) is True

    def test_wrong_secret(self):
        body = b'{"id": "evt_1"}'
        header = _stripe_sig("whsec_test", body, t=1_700_000_000)
        assert verify_stripe(
            "wrong", body, header, now=1_700_000_000
        ) is False

    def test_outside_tolerance_rejected(self):
        body = b'{"id": "evt_1"}'
        header = _stripe_sig("whsec_test", body, t=1_700_000_000)
        # 400s later, default tolerance is 300s.
        assert verify_stripe(
            "whsec_test", body, header, now=1_700_000_400
        ) is False

    def test_within_tolerance_accepted(self):
        body = b'{"id": "evt_1"}'
        header = _stripe_sig("whsec_test", body, t=1_700_000_000)
        assert verify_stripe(
            "whsec_test", body, header, now=1_700_000_200
        ) is True

    def test_missing_header(self):
        assert verify_stripe("whsec_test", b"{}", None) is False

    def test_malformed_header(self):
        assert verify_stripe("whsec_test", b"{}", "garbage") is False


class TestVerifyGeneric:
    def test_match(self):
        assert verify_generic("tok123", "tok123") is True

    def test_mismatch(self):
        assert verify_generic("tok123", "wrong") is False

    def test_missing_header(self):
        assert verify_generic("tok123", None) is False


class TestEventAllowed:
    def test_empty_allowlist_accepts_everything(self):
        wh = WebhookConfig(provider="github", secret="s", events=[])
        assert event_allowed(wh, "push") is True
        assert event_allowed(wh, None) is True

    def test_allowlist_hit(self):
        wh = WebhookConfig(provider="github", secret="s", events=["push", "pull_request"])
        assert event_allowed(wh, "push") is True

    def test_allowlist_miss(self):
        wh = WebhookConfig(provider="github", secret="s", events=["push"])
        assert event_allowed(wh, "issues") is False
        assert event_allowed(wh, None) is False


class TestResolveMapping:
    def test_dot_path(self):
        payload = {"repository": {"full_name": "acme/widgets"}, "ref": "refs/heads/main"}
        mapping = {"repo": "$.repository.full_name", "branch": "$.ref"}
        assert resolve_mapping(payload, mapping) == {
            "repo": "acme/widgets",
            "branch": "refs/heads/main",
        }

    def test_list_index(self):
        payload = {"commits": [{"id": "abc123"}, {"id": "def456"}]}
        mapping = {"first_commit": "$.commits[0].id"}
        assert resolve_mapping(payload, mapping) == {"first_commit": "abc123"}

    def test_missing_path_omitted_not_fatal(self):
        payload = {"ref": "refs/heads/main"}
        mapping = {"repo": "$.repository.full_name", "branch": "$.ref"}
        assert resolve_mapping(payload, mapping) == {"branch": "refs/heads/main"}

    def test_non_dict_payload_returns_empty(self):
        assert resolve_mapping("not a dict", {"x": "$.a"}) == {}
        assert resolve_mapping(None, {"x": "$.a"}) == {}

    def test_non_string_value_stringified(self):
        payload = {"data": {"object": {"amount": 4200}}}
        mapping = {"amount": "$.data.object.amount"}
        assert resolve_mapping(payload, mapping) == {"amount": "4200"}


class TestResolveSecret:
    def test_literal_passthrough(self):
        cfg = WebappConfig(secrets={})
        assert resolve_secret("literal-value", cfg) == "literal-value"

    def test_secret_ref_resolves(self):
        cfg = WebappConfig(secrets={"gh_key": "whsec_abc"})
        assert resolve_secret("$secret:gh_key", cfg) == "whsec_abc"

    def test_unknown_secret_ref_raises(self):
        cfg = WebappConfig(secrets={})
        with pytest.raises(ValueError):
            resolve_secret("$secret:missing", cfg)


class TestWebhookFromDict:
    def test_none_is_none(self):
        assert webhook_from_dict(None) is None

    def test_valid_roundtrip(self):
        raw = {
            "provider": "stripe",
            "secret": "$secret:stripe_key",
            "mapping": {"intent_id": "$.data.object.id"},
            "events": [],
        }
        wh = webhook_from_dict(raw)
        assert wh.provider == "stripe"
        assert wh.secret == "$secret:stripe_key"
        assert wh.mapping == {"intent_id": "$.data.object.id"}
        assert wh.to_dict() == {
            "provider": "stripe",
            "secret": "$secret:stripe_key",
            "mapping": {"intent_id": "$.data.object.id"},
        }

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError):
            webhook_from_dict({"provider": "bitbucket", "secret": "x"})

    def test_empty_secret_rejected(self):
        with pytest.raises(ValueError):
            webhook_from_dict({"provider": "generic", "secret": ""})

    def test_job_roundtrip_through_job_from_dict(self):
        raw = {
            "id": "gh-hook",
            "name": "GitHub Hook",
            "script_path": "C:/x/y.py",
            "webhook": {
                "provider": "github",
                "secret": "$secret:gh_key",
                "mapping": {"repo": "$.repository.full_name"},
                "events": ["push"],
            },
        }
        job = job_from_dict(raw)
        assert job.webhook.provider == "github"
        assert job.to_dict()["webhook"]["events"] == ["push"]
