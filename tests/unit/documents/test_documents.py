"""src.documents: the allowlist's validation and the path jail.

Every name, title and file here is invented.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src import documents
from src.documents import Document, DocumentError


def _link_dir(target: Path, link: Path) -> None:
    """A directory link to ``target``: a symlink, else a Windows junction.

    A junction needs no privilege, so the jail is still exercised on a host
    that cannot create symlinks.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError):
        pytest.skip("this host can create neither symlinks nor junctions")


def _row(**over):
    row = {"id": "guide", "title": "Guide", "repo": "alpha", "path": "docs/guide.md"}
    row.update(over)
    return row


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    """A fleet root with one repo holding one document."""
    (tmp_path / "alpha" / "docs").mkdir(parents=True)
    (tmp_path / "alpha" / "docs" / "guide.md").write_text("# Guide\n\nbody\n", encoding="utf-8")
    return tmp_path


class TestParse:
    def test_a_valid_row_loads(self):
        assert documents.parse_documents([_row()]) == [
            Document("guide", "Guide", "alpha", "docs/guide.md")
        ]

    @pytest.mark.parametrize(
        "over",
        [
            {"path": "../outside.md"},
            {"path": "docs/../../outside.md"},
            {"path": "/etc/guide.md"},
            {"path": "C:/Windows/guide.md"},
            {"path": "C:guide.md"},
            {"path": "docs\\guide.md"},
            {"path": "docs//guide.md"},
            {"path": "./guide.md"},
            {"path": "docs/guide.txt"},
            {"path": "docs/guide.md "},
            {"path": ""},
            {"repo": "../alpha"},
            {"repo": "a/b"},
            {"repo": "a\\b"},
            {"repo": ".git"},
            {"repo": ".."},
            {"repo": "alpha-wt-12"},
            {"repo": ""},
            {"id": "Has Space"},
            {"id": ""},
            {"title": "   "},
            {"title": "x" * 200},
            {"path": 7},
            {"repo": None},
        ],
    )
    def test_a_bad_row_is_dropped(self, over):
        assert documents.parse_documents([_row(**over)]) == []

    def test_a_bad_row_does_not_stop_the_rest(self):
        rows = [_row(path="../x.md"), "not an object", _row(id="second", title="Second")]
        assert [d.id for d in documents.parse_documents(rows)] == ["second"]

    def test_a_repeated_id_keeps_the_first(self):
        rows = [_row(), _row(title="Other")]
        assert [d.title for d in documents.parse_documents(rows)] == ["Guide"]

    def test_a_non_list_is_empty(self):
        assert documents.parse_documents(None) == []
        assert documents.parse_documents({"id": "guide"}) == []


class TestRead:
    def test_reads_text_and_time(self, fleet):
        doc = documents.parse_documents([_row()])[0]
        body = documents.read_document(str(fleet), doc)
        assert body["content"].startswith("# Guide")
        assert body["repo"] == "alpha"
        assert body["updated"].endswith("+00:00")
        assert documents.last_changed(str(fleet), doc) == body["updated"]

    def test_a_missing_file_says_not_found(self, fleet):
        doc = Document("nope", "Nope", "alpha", "docs/missing.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "not_found"
        assert documents.last_changed(str(fleet), doc) is None

    def test_a_missing_repo_is_not_served(self, fleet):
        doc = Document("nope", "Nope", "beta", "docs/guide.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "not_found"

    def test_an_oversize_file_is_reported_not_cut(self, fleet):
        big = fleet / "alpha" / "docs" / "big.md"
        big.write_bytes(b"x" * (documents.MAX_DOCUMENT_BYTES + 1))
        doc = Document("big", "Big", "alpha", "docs/big.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "too_large"

    def test_a_file_at_the_cap_is_served_whole(self, fleet):
        edge = fleet / "alpha" / "docs" / "edge.md"
        edge.write_bytes(b"y" * documents.MAX_DOCUMENT_BYTES)
        doc = Document("edge", "Edge", "alpha", "docs/edge.md")
        assert documents.read_document(str(fleet), doc)["bytes"] == documents.MAX_DOCUMENT_BYTES

    def test_a_directory_is_not_a_file(self, fleet):
        (fleet / "alpha" / "docs" / "folder.md").mkdir()
        doc = Document("dir", "Dir", "alpha", "docs/folder.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "not_found"

    def test_a_path_that_dodges_validation_is_still_jailed(self, fleet):
        # Built by hand, bypassing parse_documents: the jail is a second layer.
        (fleet / "secret.md").write_text("outside", encoding="utf-8")
        doc = Document("evil", "Evil", "alpha", "../secret.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "outside"

    def test_a_symlink_pointing_out_of_the_repo_is_refused(self, fleet, tmp_path_factory):
        outside = tmp_path_factory.mktemp("elsewhere") / "secret.md"
        outside.write_text("outside", encoding="utf-8")
        link = fleet / "alpha" / "docs" / "link.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create symlinks")
        doc = Document("link", "Link", "alpha", "docs/link.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "outside"
        assert documents.last_changed(str(fleet), doc) is None

    def test_a_folder_link_inside_the_repo_cannot_reach_out(self, fleet, tmp_path_factory):
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        (elsewhere / "secret.md").write_text("outside", encoding="utf-8")
        _link_dir(elsewhere, fleet / "alpha" / "docs" / "out")
        doc = Document("via", "Via", "alpha", "docs/out/secret.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "outside"

    def test_a_repo_folder_that_links_out_of_the_fleet_is_refused(self, fleet, tmp_path_factory):
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        (elsewhere / "docs").mkdir()
        (elsewhere / "docs" / "guide.md").write_text("outside", encoding="utf-8")
        _link_dir(elsewhere, fleet / "gamma")
        doc = Document("g", "G", "gamma", "docs/guide.md")
        with pytest.raises(DocumentError) as exc:
            documents.read_document(str(fleet), doc)
        assert exc.value.kind == "outside"


def test_listing_marks_an_unreadable_file_without_dropping_the_row(fleet):
    docs = documents.parse_documents([_row(), _row(id="gone", title="Gone", path="docs/gone.md")])
    rows = documents.list_documents(str(fleet), docs)
    assert [r["id"] for r in rows] == ["guide", "gone"]
    assert rows[0]["updated"] and rows[1]["updated"] is None
    assert set(rows[0]) == {"id", "title", "repo", "updated"}
