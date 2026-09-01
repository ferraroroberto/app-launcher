"""Rebuildable SQLite/FTS mirror for Jobs-tab run history.

The plain ``run.json`` / ``output.log`` files remain canonical.  This module
only owns a derived query index; deleting it is safe, and the next read or
write rebuilds it from the filesystem.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import jobs_history
from src.runtime_data import runtime_data_dir

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
INDEX_FILENAME = "_index.sqlite"
_INDEX_LOCK = threading.RLock()

# Where the index file lives. It used to sit inside the run-history root it
# mirrors (``webapp/jobs/``), which put its writes on whichever drive this repo
# was cloned onto — a spinning HDD here (project-scaffolding#243). It now
# resolves to the fleet runtime-data root instead.
#
# A module global rather than a call to ``runtime_data_dir()`` inside
# :func:`index_path`, because the tests need one place to redirect: isolation
# used to come for free from patching ``jobs_history.JOBS_RUNS_DIR``, and
# decoupling the two would otherwise have pointed every one of those tests at
# the real production index. ``tests/conftest.py`` patches this autouse.
INDEX_DIR = runtime_data_dir("app-launcher")


def index_path() -> Path:
    """Return the index path for the active run-history root.

    The index is a *derived* mirror — deleting it is safe and the next read or
    write rebuilds it from ``JOBS_RUNS_DIR`` — so it is free to live on a
    different volume from the run records it indexes.
    """
    return INDEX_DIR / INDEX_FILENAME


def _connect() -> sqlite3.Connection:
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        return False
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('runs', 'output_fts')"
        )
    }
    return names == {"runs", "output_fts"}


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS output_fts;
        DROP TABLE IF EXISTS runs;
        CREATE TABLE runs (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            status TEXT,
            started_at TEXT,
            finished_at TEXT,
            duration_seconds REAL,
            exit_code INTEGER,
            trigger TEXT,
            pinned INTEGER NOT NULL DEFAULT 0,
            output_size INTEGER NOT NULL DEFAULT 0,
            has_artifacts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(job_id, run_id)
        );
        CREATE INDEX runs_job_started_idx ON runs(job_id, started_at DESC);
        CREATE INDEX runs_status_started_idx ON runs(status, started_at DESC);
        CREATE VIRTUAL TABLE output_fts USING fts5(content);
        """
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _read_record(run_dir: Path) -> Dict[str, Any]:
    return jobs_history.read_run(run_dir)


def _artifact_present(run_dir: Path) -> bool:
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return False
    try:
        return any(path.is_file() for path in artifacts_dir.rglob("*"))
    except OSError:
        return False


def _sync_run_conn(
    conn: sqlite3.Connection,
    run_dir: Path,
    record: Optional[Dict[str, Any]] = None,
) -> None:
    record = dict(record or _read_record(run_dir))
    if not record:
        return
    job_id = str(record.get("job_id") or run_dir.parent.name)
    run_id = str(record.get("run_id") or run_dir.name)
    output_path = run_dir / "output.log"
    try:
        output_size = output_path.stat().st_size if output_path.is_file() else 0
        output = output_path.read_text(encoding="utf-8", errors="replace") if output_size else ""
    except OSError:
        output_size = 0
        output = ""
    conn.execute(
        """
        INSERT INTO runs (
            job_id, run_id, status, started_at, finished_at, duration_seconds,
            exit_code, trigger, pinned, output_size, has_artifacts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, run_id) DO UPDATE SET
            status=excluded.status,
            started_at=excluded.started_at,
            finished_at=excluded.finished_at,
            duration_seconds=excluded.duration_seconds,
            exit_code=excluded.exit_code,
            trigger=excluded.trigger,
            pinned=excluded.pinned,
            output_size=excluded.output_size,
            has_artifacts=excluded.has_artifacts
        """,
        (
            job_id,
            run_id,
            record.get("status"),
            record.get("started_at"),
            record.get("finished_at"),
            record.get("duration_seconds"),
            record.get("exit_code"),
            record.get("trigger"),
            int(bool(record.get("pinned"))),
            output_size,
            int(_artifact_present(run_dir)),
        ),
    )
    row = conn.execute(
        "SELECT rowid FROM runs WHERE job_id=? AND run_id=?", (job_id, run_id)
    ).fetchone()
    if row is None:
        return
    rowid = int(row[0])
    conn.execute("DELETE FROM output_fts WHERE rowid=?", (rowid,))
    conn.execute("INSERT INTO output_fts(rowid, content) VALUES (?, ?)", (rowid, output))


def _rebuild_conn(conn: sqlite3.Connection) -> None:
    _create_schema(conn)
    root = jobs_history.JOBS_RUNS_DIR
    if root.is_dir():
        for job_dir in root.iterdir():
            if not job_dir.is_dir():
                continue
            for run_dir in job_dir.iterdir():
                if run_dir.is_dir():
                    _sync_run_conn(conn, run_dir)
    conn.commit()
    logger.info("ℹ️ rebuilt Jobs run index at %s", index_path())


def ensure_index() -> None:
    """Create or transparently rebuild a missing/stale/corrupt index."""
    with _INDEX_LOCK:
        try:
            with _connect() as conn:
                if not _schema_is_current(conn):
                    _rebuild_conn(conn)
        except sqlite3.DatabaseError as exc:
            path = index_path()
            logger.warning("⚠️ Jobs index corrupt at %s — rebuilding: %s", path, exc)
            try:
                path.unlink(missing_ok=True)
                Path(str(path) + "-wal").unlink(missing_ok=True)
                Path(str(path) + "-shm").unlink(missing_ok=True)
            except OSError:
                raise
            with _connect() as conn:
                _rebuild_conn(conn)


def sync_run(run_dir: Path, record: Optional[Dict[str, Any]] = None) -> None:
    """Mirror one canonical run into SQLite after a file write."""
    with _INDEX_LOCK:
        ensure_index()
        with _connect() as conn:
            _sync_run_conn(conn, run_dir, record)
            conn.commit()


def remove_run(job_id: str, run_id: str) -> None:
    """Remove one pruned run from the derived mirror."""
    with _INDEX_LOCK:
        ensure_index()
        with _connect() as conn:
            row = conn.execute(
                "SELECT rowid FROM runs WHERE job_id=? AND run_id=?", (job_id, run_id)
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM output_fts WHERE rowid=?", (int(row[0]),))
                conn.execute(
                    "DELETE FROM runs WHERE job_id=? AND run_id=?", (job_id, run_id)
                )
            conn.commit()


def run_counts(job_id: str) -> Tuple[int, int]:
    """Return ``(kept_count, pinned_count)`` for one job."""
    ensure_index()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pinned), 0) FROM runs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def run_counts_by_job() -> Dict[str, Tuple[int, int]]:
    """Return all per-job ``(kept, pinned)`` counts in one indexed read."""
    ensure_index()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT job_id, COUNT(*) AS kept, COALESCE(SUM(pinned), 0) AS pinned
            FROM runs
            GROUP BY job_id
            """
        ).fetchall()
    return {
        str(row["job_id"]): (int(row["kept"]), int(row["pinned"]))
        for row in rows
    }


def search_runs(
    query: str,
    *,
    job_id: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """FTS-search output, newest first, with optional exact filters."""
    tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)
    if not tokens:
        return []
    match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    clauses = ["output_fts MATCH ?"]
    params: List[Any] = [match]
    if job_id:
        clauses.append("runs.job_id = ?")
        params.append(job_id)
    if status:
        clauses.append("runs.status = ?")
        params.append(status)
    if since:
        clauses.append("runs.started_at >= ?")
        params.append(since)
    params.append(max(1, min(int(limit), 200)))
    ensure_index()
    sql = f"""
        SELECT runs.job_id, runs.run_id, runs.status, runs.started_at,
               snippet(output_fts, 0, '', '', ' … ', 18) AS snippet
        FROM output_fts
        JOIN runs ON runs.rowid = output_fts.rowid
        WHERE {' AND '.join(clauses)}
        ORDER BY runs.started_at DESC, bm25(output_fts)
        LIMIT ?
    """
    with _connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
