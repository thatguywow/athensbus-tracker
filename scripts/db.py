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
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Apply safe additive migrations (add columns if missing)."""
    def add_column(table, column, decl):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except Exception:
                pass

    # Persistent median route trip duration (for departure extrapolation)
    add_column("route_rotation", "median_trip_duration_mins", "REAL")
    add_column("route_rotation", "duration_samples", "TEXT")

    # stop_passages table (exact pass times via disappearance detection)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stop_passages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            route_code   TEXT NOT NULL,
            stop_code    TEXT NOT NULL,
            stop_type    TEXT NOT NULL,
            stop_order   INTEGER,
            vehicle_no   TEXT NOT NULL,
            passed_at    TEXT NOT NULL,
            service_date TEXT NOT NULL,
            recorded_at  TEXT NOT NULL,
            UNIQUE(route_code, stop_code, vehicle_no, passed_at)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_passages_route_date ON stop_passages(route_code, service_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_passages_vehicle ON stop_passages(vehicle_no, service_date)")
    add_column("stop_passages", "stop_order", "INTEGER")

    # Normal (theoretical) timetable — the standard schedule that SHOULD run,
    # separate from the daily revised plan in scheduled_trips. Enables the
    # three-way comparison: Normal vs Daily vs Executed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS normal_schedule (
            route_code     TEXT NOT NULL,
            schedule_date  TEXT NOT NULL,
            departure_time TEXT NOT NULL,
            sdc_code       TEXT,
            last_synced    TEXT NOT NULL,
            UNIQUE(route_code, schedule_date, departure_time)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_normal_sched_route_date ON normal_schedule(route_code, schedule_date)")

    # Persistent per-segment travel time: median minutes from the ORIGIN to each
    # near-origin stop_order, accumulated across days. Used to subtract the REAL
    # origin→stop offset when back-calculating departure (instead of assuming
    # uniform speed), eliminating the small early-bias.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS segment_times (
            route_code   TEXT NOT NULL,
            stop_order   INTEGER NOT NULL,
            median_mins  REAL,
            samples      TEXT,
            last_updated TEXT NOT NULL,
            UNIQUE(route_code, stop_order)
        )
    """)


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
