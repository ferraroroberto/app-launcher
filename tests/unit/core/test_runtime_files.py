"""`src/runtime_files.py` refuses a destination under a code folder and writes elsewhere."""

from __future__ import annotations

import sqlite3

import pytest

from src import runtime_files


def test_writes_outside_the_code_folders(tmp_path):
    target = tmp_path / "state" / "a.txt"
    runtime_files.make_dirs(target.parent)
    runtime_files.save_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    runtime_files.save_bytes(tmp_path / "b.bin", b"\x00\x01")
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01"


@pytest.mark.parametrize("folder", runtime_files.CODE_FOLDERS)
def test_every_writer_refuses_a_code_folder(folder):
    inside = runtime_files.REPO_ROOT / folder / "_never_written" / "x"
    for call in (
        lambda: runtime_files.make_dirs(inside),
        lambda: runtime_files.open_for_write(inside),
        lambda: runtime_files.save_text(inside, "x"),
        lambda: runtime_files.save_bytes(inside, b"x"),
        lambda: runtime_files.file_handler(inside),
        lambda: runtime_files.connect_sqlite(inside),
        lambda: runtime_files.replace(inside, inside),
        lambda: runtime_files.move(inside, inside),
    ):
        with pytest.raises(ValueError, match="code folder"):
            call()
    assert not (runtime_files.REPO_ROOT / folder / "_never_written").exists()


def test_the_code_folder_itself_is_refused():
    with pytest.raises(ValueError):
        runtime_files.checked(runtime_files.REPO_ROOT / "src")


def test_sqlite_and_swap_helpers_work_outside(tmp_path):
    db = tmp_path / "db.sqlite3"
    conn = runtime_files.connect_sqlite(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    ro = runtime_files.connect_sqlite_read_only(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
    ro.close()

    src = tmp_path / "src.txt"
    src.write_text("new", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    dst.write_text("old", encoding="utf-8")
    runtime_files.replace(src, dst)
    assert dst.read_text(encoding="utf-8") == "new"
    moved = runtime_files.move(dst, tmp_path / "moved.txt")
    assert moved.read_text(encoding="utf-8") == "new"
