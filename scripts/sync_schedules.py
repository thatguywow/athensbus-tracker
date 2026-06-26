"""
sync_schedules.py — daily job.

Pulls getDailySchedule per line and stores theoretical departure times.
Strictly filters to valid service hours (04:00-23:59) and clean HH:MM:SS format.
Ignores midnight/invalid entries that OASA sometimes returns.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, date, time

import db
import oasa_client as oasa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_schedules")

# Valid service window — anything outside this is an OASA data artifact
SERVICE_START = time(4, 0)   # 04:00
SERVICE_END   = time(23, 59) # 23:59


def is_valid_departure(t_str: str) -> bool:
    """Accept only clean HH:MM:SS times within the service window."""
    try:
        t = datetime.strptime(t_str, "%H:%M:%S").time()
        return SERVICE_START <= t <= SERVICE_END
    except ValueError:
        return False


def extract_departure_times(entries: list[dict]) -> list[tuple[str, str]]:
    """
    Extract (sdd_code, HH:MM:SS) pairs from getDailySchedule entries.
    Only returns times within the valid service window.
    """
    out = []
    for e in entries:
        sdd_code = str(e.get("sdd_code") or "")
        for field in ("sde_start1", "sde_start2"):
            raw = e.get(field)
            if not raw:
                continue
            try:
                t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").time()
                t_str = t.strftime("%H:%M:%S")
                if is_valid_departure(t_str):
                    out.append((sdd_code, t_str))
            except ValueError:
                continue
    return out


def main():
    db.ensure_schema()
    synced_at = db.now_utc_iso()
    today = date.today().isoformat()

    with db.job_run("sync_schedules") as run:
        conn = db.get_connection()
        try:
            line_rows = conn.execute("SELECT line_code FROM lines").fetchall()
            line_codes = [r["line_code"] for r in line_rows]
            log.info("Syncing schedules for %d lines", len(line_codes))

            route_rows = conn.execute(
                "SELECT route_code, line_code, route_type FROM routes"
            ).fetchall()
            routes_by_line: dict[str, list] = {}
            for r in route_rows:
                routes_by_line.setdefault(r["line_code"], []).append(r)

            total_inserted = 0
            failed = []

            for i, line_code in enumerate(line_codes, 1):
                try:
                    sched = oasa.get_daily_schedule(line_code)
                except Exception as e:
                    failed.append(line_code)
                    continue

                routes_for_line = routes_by_line.get(line_code, [])
                come_route = next(
                    (r for r in routes_for_line if r["route_type"] == "2"), None
                )
                go_route = next(
                    (r for r in routes_for_line if r["route_type"] == "1"), None
                )

                for direction_key, route in (("come", come_route), ("go", go_route)):
                    if route is None:
                        continue
                    entries = sched.get(direction_key) or []
                    times = extract_departure_times(entries)

                    # Clear today's existing rows for this route so a re-sync
                    # produces a clean schedule (no stale duplicates).
                    conn.execute(
                        "DELETE FROM scheduled_trips WHERE route_code=? AND schedule_date=?",
                        (route["route_code"], today)
                    )

                    # Deduplicate by departure_time — OASA sometimes returns the
                    # same departure twice with different sdd_codes (08:25, 08:25).
                    seen_times = set()
                    for sdd_code, dep_time in times:
                        if dep_time in seen_times:
                            continue
                        seen_times.add(dep_time)
                        conn.execute(
                            """
                            INSERT INTO scheduled_trips
                                (route_code, schedule_date, departure_time,
                                 raw_sdd_code, last_synced)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(route_code, schedule_date,
                                        departure_time, raw_sdd_code)
                            DO UPDATE SET last_synced = excluded.last_synced
                            """,
                            (route["route_code"], today, dep_time,
                             sdd_code, synced_at),
                        )
                        total_inserted += 1

                if i % 50 == 0:
                    conn.commit()
                    log.info("Progress: %d/%d lines", i, len(line_codes))

            conn.commit()
            run.detail = (
                f"date={today} schedule_rows={total_inserted} "
                f"failed_lines={len(failed)}"
            )
            if failed:
                run.status = "partial"
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
