"""
compute_daily_report.py — daily orchestration job (runs on GitHub Actions).

Processes TODAY's data (not yesterday) since it runs every hour.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta, datetime, timezone

import db
from trip_reconstruction import reconstruct_route_day
from rotation_slots import compute_all_slots

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("compute_daily_report")

RETENTION_DAYS = 30


def target_service_date() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return date.today().isoformat()  # always today for live updates


def count_scheduled(conn, route_code: str, service_date: str) -> int:
    r = conn.execute(
        "SELECT COUNT(*) c FROM scheduled_trips WHERE route_code=? AND schedule_date=?",
        (route_code, service_date)
    ).fetchone()
    return r["c"] if r else 0


def purge_old_data(conn, retention_days: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    p1 = conn.execute("DELETE FROM vehicle_pings WHERE ts_utc < ?", (cutoff,)).rowcount
    p2 = conn.execute("DELETE FROM terminus_observations WHERE observed_at < ?", (cutoff,)).rowcount
    return {"pings": p1, "terminus_obs": p2}


def main():
    db.ensure_schema()
    service_date = target_service_date()
    computed_at  = db.now_utc_iso()

    with db.job_run("compute_daily_report") as run:
        conn = db.get_connection()
        try:
            route_rows  = conn.execute("SELECT route_code FROM routes").fetchall()
            route_codes = [r["route_code"] for r in route_rows]
            log.info("Computing report for %s across %d routes", service_date, len(route_codes))

            total_trips = total_departures = 0
            errors = []

            for i, rc in enumerate(route_codes, 1):
                try:
                    s = reconstruct_route_day(conn, rc, service_date, computed_at)
                    total_trips      += s["trips"]
                    total_departures += s["departures"]
                except Exception as e:
                    log.warning("Trip reconstruction failed for %s: %s", rc, e)
                    errors.append(rc)
                if i % 50 == 0:
                    conn.commit()
                    log.info("Trips: %d/%d routes", i, len(route_codes))
            conn.commit()

            log.info("Computing rotation slots...")
            slot_stats = compute_all_slots(conn, service_date, computed_at)
            conn.commit()

            for rc in route_codes:
                try:
                    actual = conn.execute(
                        "SELECT COUNT(*) c FROM trips WHERE route_code=? AND service_date=?",
                        (rc, service_date)
                    ).fetchone()["c"]
                    scheduled = count_scheduled(conn, rc, service_date)
                    completion = round(actual / scheduled * 100, 1) if scheduled > 0 else None

                    avg_dev = conn.execute("""
                        SELECT AVG(sa.departure_deviation_mins)
                        FROM slot_assignments sa
                        JOIN trips t ON t.id = sa.trip_id
                        WHERE t.route_code=? AND t.service_date=?
                    """, (rc, service_date)).fetchone()[0]

                    slot_count = conn.execute("""
                        SELECT slot_count FROM rotation_patterns
                        WHERE route_code=? AND service_date=?
                    """, (rc, service_date)).fetchone()

                    distinct = conn.execute("""
                        SELECT COUNT(DISTINCT vehicle_no) c FROM trips
                        WHERE route_code=? AND service_date=?
                    """, (rc, service_date)).fetchone()["c"]

                    conn.execute("""
                        INSERT INTO daily_route_stats
                            (route_code, service_date, actual_trip_count,
                             scheduled_trip_count, completion_pct,
                             distinct_vehicles, avg_deviation_mins,
                             slot_count, computed_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(route_code, service_date) DO UPDATE SET
                            actual_trip_count    = excluded.actual_trip_count,
                            scheduled_trip_count = excluded.scheduled_trip_count,
                            completion_pct       = excluded.completion_pct,
                            distinct_vehicles    = excluded.distinct_vehicles,
                            avg_deviation_mins   = excluded.avg_deviation_mins,
                            slot_count           = excluded.slot_count,
                            computed_at          = excluded.computed_at
                    """, (rc, service_date, actual, scheduled, completion,
                          distinct,
                          round(avg_dev, 1) if avg_dev is not None else None,
                          slot_count["slot_count"] if slot_count else None,
                          computed_at))
                except Exception as e:
                    log.warning("Stats rollup failed for %s: %s", rc, e)
            conn.commit()

            purged = purge_old_data(conn, RETENTION_DAYS)
            conn.commit()

            run.detail = (
                f"date={service_date} trips={total_trips} "
                f"departures={total_departures} "
                f"slots_assigned={slot_stats['assigned']} "
                f"handoffs={slot_stats['handoffs']} "
                f"errors={len(errors)} "
                f"purged_pings={purged['pings']}"
            )
            if errors:
                run.status = "partial"
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
