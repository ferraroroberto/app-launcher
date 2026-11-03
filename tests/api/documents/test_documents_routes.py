"""GET /api/documents and /api/documents/{id}: the allowlist and its gate.

Every name, title and file here is invented.
"""

from __future__ import annotations

import pytest

from app.webapp.middleware.routes import terminal_guard_level


@pytest.fixture
def docs_env(webapp_client, tmp_path):
    client, app, _ = webapp_client
    fleet = tmp_path / "fleet"
    (fleet / "alpha" / "docs").mkdir(parents=True)
    (fleet / "alpha" / "docs" / "guide.md").write_text(
        "# Guide\n\n```mermaid\ngraph TD\n  A --> B\n```\n", encoding="utf-8"
    )
    (fleet / "secret.md").write_text("not on the list", encoding="utf-8")
    cfg = app.state.webapp_config
    cfg.projects_dir = str(fleet)
    cfg.documents = [
        {"id": "guide", "title": "Guide", "repo": "alpha", "path": "docs/guide.md"},
        {"id": "gone", "title": "Gone", "repo": "alpha", "path": "docs/gone.md"},
        {"id": "escape", "title": "Escape", "repo": "alpha", "path": "../secret.md"},
        {"id": "worktree", "title": "Tree", "repo": "alpha-wt-9", "path": "docs/guide.md"},
    ]
    return client, cfg, fleet


@pytest.fixture
def docs_client(docs_env, monkeypatch):
    """The same, with the test client treated as the PC itself so the terminal
    gate steps aside and the endpoint logic is exercised; the gate has its own
    tests below."""
    from app.webapp import middleware

    monkeypatch.setattr(
        middleware.trust,
        "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )
    return docs_env


def test_the_list_carries_only_valid_rows_and_no_text(docs_client):
    client, _, _ = docs_client
    body = client.get("/api/documents").json()
    assert [d["id"] for d in body["documents"]] == ["guide", "gone"]
    assert body["documents"][0]["updated"] and body["documents"][1]["updated"] is None
    assert "content" not in body["documents"][0]


def test_an_empty_config_lists_nothing(docs_client):
    client, cfg, _ = docs_client
    cfg.documents = []
    assert client.get("/api/documents").json() == {"documents": []}


def test_a_listed_document_is_served(docs_client):
    client, _, _ = docs_client
    resp = client.get("/api/documents/guide")
    assert resp.status_code == 200, resp.text
    assert "```mermaid" in resp.json()["content"]


@pytest.mark.parametrize(
    "doc_id",
    ["secret", "..%2Fsecret", "alpha%2Fdocs%2Fguide.md", "escape", "worktree"],
)
def test_an_id_off_the_list_or_dropped_by_validation_is_refused(docs_client, doc_id):
    client, _, _ = docs_client
    assert client.get(f"/api/documents/{doc_id}").status_code == 404


def test_a_path_in_the_url_reaches_no_file(docs_client):
    client, _, _ = docs_client
    assert client.get("/api/documents/docs/guide.md").status_code == 404
    assert client.get("/api/documents/../../secret.md").status_code == 404


def test_a_missing_file_is_not_found_with_its_own_wording(docs_client):
    client, _, _ = docs_client
    resp = client.get("/api/documents/gone")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "file not found"
    assert client.get("/api/documents/nothing").json()["detail"] == "no such document"


def test_an_oversize_file_is_413_not_truncated(docs_client):
    client, cfg, fleet = docs_client
    (fleet / "alpha" / "docs" / "big.md").write_bytes(b"x" * (512 * 1024 + 1))
    cfg.documents = [{"id": "big", "title": "Big", "repo": "alpha", "path": "docs/big.md"}]
    resp = client.get("/api/documents/big")
    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"]


def test_the_text_is_passkey_gated_and_the_list_is_not():
    assert terminal_guard_level("/api/documents/guide") == "passkey"
    assert terminal_guard_level("/api/documents") is None


def test_off_the_tailnet_the_text_is_refused_and_the_list_is_not(docs_env):
    client, _, _ = docs_env
    assert client.get("/api/documents/guide").status_code == 403
    assert client.get("/api/documents").status_code == 200


def test_over_the_public_tunnel_the_text_is_refused(docs_env):
    client, _, _ = docs_env
    resp = client.get("/api/documents/guide", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert resp.status_code == 403
