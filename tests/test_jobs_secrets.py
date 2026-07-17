"""``$secret:<key>`` resolution + ``Job.env`` shape validation (issue #72)."""

from __future__ import annotations

import pytest

from src.jobs_config import env_from_dict
from src.jobs_secrets import resolve_env_overlay, resolve_secret_value


class TestResolveSecretValue:
    def test_literal_passthrough(self):
        assert resolve_secret_value("plain", {}) == "plain"

    def test_ref_resolves(self):
        assert resolve_secret_value("$secret:k", {"k": "v"}) == "v"

    def test_unknown_ref_raises_with_key_in_message(self):
        with pytest.raises(ValueError, match=r"secret 'nope' not found"):
            resolve_secret_value("$secret:nope", {"k": "v"})


class TestResolveEnvOverlay:
    def test_mixed_literals_and_refs(self):
        env = {"PLAIN": "x", "SECRET": "$secret:api"}
        out = resolve_env_overlay(env, {"api": "resolved"})
        assert out == {"PLAIN": "x", "SECRET": "resolved"}

    def test_returns_fresh_dict(self):
        env = {"A": "1"}
        out = resolve_env_overlay(env, {})
        out["A"] = "mutated"
        assert env["A"] == "1"

    def test_first_missing_ref_raises(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_env_overlay({"A": "$secret:gone"}, {})


class TestEnvFromDict:
    def test_none_is_empty(self):
        assert env_from_dict(None) == {}

    def test_valid_shape(self):
        assert env_from_dict({"API_KEY": "$secret:k"}) == {"API_KEY": "$secret:k"}

    def test_non_dict_rejected(self):
        with pytest.raises(ValueError, match="env must be an object"):
            env_from_dict(["A=B"])

    def test_lowercase_name_rejected(self):
        with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
            env_from_dict({"api_key": "v"})

    def test_non_string_value_rejected(self):
        with pytest.raises(ValueError, match="must be a string"):
            env_from_dict({"API_KEY": 42})
