"""``is_link`` is the one symlink-or-junction test the path jails share.

A second copy that drifts is a hole in a jail, so a structural check pins that
no other module in ``src`` re-derives it.
"""

import re
from pathlib import Path

import pytest

from src.life_os import links
from src.life_os.links import is_link

SRC = Path(__file__).resolve().parent.parent.parent.parent / "src"
OWN_COPY = re.compile(r"isjunction|\.is_junction\(")


def test_a_plain_file_and_folder_are_not_links(tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    assert not is_link(tmp_path)
    assert not is_link(tmp_path / "f.txt")
    assert not is_link(tmp_path / "missing")


def test_a_symlink_is_a_link(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    assert is_link(link)


def test_a_junction_is_a_link(tmp_path, monkeypatch):
    monkeypatch.setattr(links.os.path, "isjunction", lambda _p: True, raising=False)
    assert is_link(tmp_path)


def test_no_other_module_re_derives_the_link_predicate():
    offenders = [
        str(path.relative_to(SRC.parent))
        for path in SRC.rglob("*.py")
        if path != SRC / "life_os" / "links.py" and OWN_COPY.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, "use src.life_os.links.is_link: " + ", ".join(offenders)
