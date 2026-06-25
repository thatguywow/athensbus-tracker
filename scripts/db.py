"""
db.py — SQLite connection helper + job-run bookkeeping.
"""

from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("ATHENSBUS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "db", "athensbus.db"
))
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema():
    conn = get_connection()
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def job_run(job_name: str):
    """
    Context manager that records a job_runs row: start time, end time, status,
    and a free-form detail string. Use like:

        with job_run("poll_live") as run:
            ... do work ...
            run.detail = f"polled {n} routes, {failed} failed"
            run.status = "success"

    If the block raises, status is recorded as 'error' with the exception text,
    and the exception is re-raised (so CI step still fails visibly).
    """
    conn = get_connection()
    started_at = now_utc_iso()
    cur = conn.execute(
        "INSERT INTO job_runs (job_name, started_at, status) VALUES (?, ?, 'running')",
        (job_name, started_at),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()

    class _Run:
        status = "success"
        detail = ""

    run = _Run()
    try:
        yield run
    except Exception as e:
        run.status = "error"
        run.detail = f"{run.detail} | EXCEPTION: {e}".strip(" |")
        raise
    finally:
        conn = get_connection()
        conn.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (now_utc_iso(), run.status, run.detail, run_id),
        )
        conn.commit()
        conn.close()
