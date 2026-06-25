"""
sync_schedules.py — daily job.

Pulls getDailySchedule per line and stores theoretical departure times into
scheduled_trips. This feeds the "actual vs theoretical %" stat.

NOTE: getDailySchedule's response shape is messy/legacy (OASA's own API
quirk — see docs). We defensively extract whatever start times we can find
from both the "come" and "go" arrays, and store one scheduled_trips row per
distinct departure time found. If a line's schedule can't be parsed cleanly,
we skip it and log it rather than crash the whole run — partial schedule
data is still useful, and actual-trip counts (from GPS) don't depend on this.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, date

import db
import oasa_client as oasa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_schedules")


def extract_departure_times(entries: list[dict]) -> list[tuple[str, str]]:
    """
    From a list of getDailySchedule entries (the 'come' or 'go' array),
    extract (sdd_code, HH:MM:SS) pairs for whichever start/end time fields
    are populated. OASA's schema has sde_start1/sde_end1/sde_start2/sde_end2
    as candidate fields, not all of which are always present.
    """
    out = []
    for e in entries:
        sdd_code = str(e.get("sdd_code") or "")
        for field in ("sde_start1", "sde_start2"):
            raw = e.get(field)
            if not raw:
                continue
            # format observed: '1900-01-01 05:00:00'
            try:
                t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").time()
                out.append((sdd_code, t.strftime("%H:%M:%S")))
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

            # Map line_code -> route_codes (both directions) so we can attach
            # schedule entries to a specific route, not just a line.
            route_rows = conn.execute("SELECT route_code, line_code, route_type FROM routes").fetchall()
            routes_by_line: dict[str, list[sqlite3.Row]] = {}
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
                # route_type '2' = "come" (inbound), '1' = "go" (outbound) per OASA convention
                come_route = next((r for r in routes_for_line if r["route_type"] == "2"), None)
                go_route = next((r for r in routes_for_line if r["route_type"] == "1"), None)

                for direction_key, route in (("come", come_route), ("go", go_route)):
                    if route is None:
                        continue
                    entries = sched.get(direction_key) or []
                    times = extract_departure_times(entries)
                    for sdd_code, dep_time in times:
                        conn.execute(
                            """
                            INSERT INTO scheduled_trips
                                (route_code, schedule_date, departure_time, raw_sdd_code, last_synced)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(route_code, schedule_date, departure_time, raw_sdd_code)
                            DO UPDATE SET last_synced = excluded.last_synced
                            """,
                            (route["route_code"], today, dep_time, sdd_code, synced_at),
                        )
                        total_inserted += 1

                if i % 50 == 0:
                    conn.commit()
                    log.info("Progress: %d/%d lines", i, len(line_codes))

            conn.commit()
            run.detail = f"date={today} schedule_rows={total_inserted} failed_lines={len(failed)}"
            if failed:
                run.status = "partial"
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
