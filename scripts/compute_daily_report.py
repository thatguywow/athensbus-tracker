"""
compute_daily_report.py — daily orchestration job (runs on GitHub Actions).

Processes TODAY's data (not yesterday) since it runs every hour.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta, datetime, timezone

import db
from trip_reconstruction_passages import reconstruct_route_day_from_passages as reconstruct_route_day
from rotation_slots import compute_all_slots
from audit_day import run_audit, purge_audit
from chain_consistency import tighten_chain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("compute_daily_report")

RETENTION_DAYS = 30


def target_service_date() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    # Service day 04:00→04:00 Athens: an hourly run at e.g. 01:00 must keep
    # computing YESTERDAY's service (night buses still running), not today's.
    return db.athens_service_date()


def count_scheduled(conn, route_code: str, service_date: str) -> int:
    r = conn.execute(
        "SELECT COUNT(*) c FROM scheduled_trips WHERE route_code=? AND schedule_date=?",
        (route_code, service_date)
    ).fetchone()
    return r["c"] if r else 0


# Τα ακατέργαστα στίγματα GPS είναι ΔΙΑΓΝΩΣΤΙΚΑ, όχι πηγή αλήθειας: ο poller
# βγάζει τις διελεύσεις στη ροή και αυτές αποθηκεύονται μόνιμα. Κρατώντας τα
# στίγματα 30 ημέρες όπως όλα τα υπόλοιπα, ο πίνακας θα έφτανε ~2,3 εκατ.
# σειρές/ημέρα × 30 ≈ 69 εκατ. σειρές — αρκετό για να γεμίσει ο δίσκος ενός
# μικρού VPS. Δύο ημέρες φτάνουν για επανεκτέλεση και έλεγχο.
PING_RETENTION_HOURS = float(os.environ.get("ATHENSBUS_PING_RETENTION_HOURS", "48"))


def purge_old_data(conn, retention_days: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    purge_audit(conn, cutoff[:10])
    ping_cutoff = (datetime.now(timezone.utc)
                   - timedelta(hours=PING_RETENTION_HOURS)).isoformat()
    p1 = conn.execute("DELETE FROM vehicle_pings WHERE ts_utc < ?",
                      (ping_cutoff,)).rowcount
    p2 = conn.execute("DELETE FROM terminus_observations WHERE observed_at < ?", (cutoff,)).rowcount
    p3 = conn.execute("DELETE FROM stop_passages WHERE passed_at < ?", (cutoff,)).rowcount
    return {"pings": p1, "terminus_obs": p2, "passages": p3}


def main():
    db.ensure_schema()
    service_date = target_service_date()
    computed_at  = db.now_utc_iso()

    # ── Handover window: for a few hours after the 04:00 day flip, ALSO
    # recompute YESTERDAY. Trips that departed near/before 04:00 finish after
    # it (e.g. dep 03:55 → arr 04:49); yesterday's last regular compute ran
    # before they completed, so without this pass they would never be stored
    # on their owner day (today's reconstruction rightly skips them).
    dates_to_compute = [service_date]
    if len(sys.argv) <= 1:   # only for automatic runs, not explicit dates
        try:
            from zoneinfo import ZoneInfo
            athens_hour = datetime.now(ZoneInfo("Europe/Athens")).hour
        except Exception:
            athens_hour = 12
        if 4 <= athens_hour < 7:
            prev = (date.fromisoformat(service_date) - timedelta(days=1)).isoformat()
            dates_to_compute.append(prev)

    # Manual runs with an explicit date are authoritative → full recompute.
    # ATHENSBUS_FULL_COMPUTE=1 forces it for automatic runs too.
    full = (len(sys.argv) > 1
            or os.environ.get("ATHENSBUS_FULL_COMPUTE") == "1")
    for service_date in dates_to_compute:
        _compute_one_day(service_date, computed_at, full=full)


def _routes_needing_recompute(conn, route_codes: list[str],
                             service_date: str) -> list[str]:
    """
    Return the routes whose stored result for `service_date` can still differ
    from a fresh computation — i.e. the routes whose INPUTS changed since the
    last time they were computed. Everything else is skipped: reconstruction is
    deterministic, so identical inputs necessarily yield identical output.

    A route is stale when any of these holds:
      1. it has no stored result yet (never computed for this day);
      2. a passage inside the day's reconstruction window was recorded after
         that result was computed (new/late data — including trips that only
         finished after the 04:00 flip);
      3. its OWN day's schedule changed after that result (the slot grid and
         the boundary arbitration both read it);
      4. a NEIGHBOURING day's schedule changed AND the route has passages in a
         boundary zone — the only case where the other day's schedule can flip
         a trip's ownership.

    Cost: five set-queries per day instead of per-route probes.
    """
    from trip_reconstruction_passages import (passage_query_window,
                                              boundary_zone_windows,
                                              passage_method_sql)

    # Οι διελεύσεις που ΔΕΝ διαβάζει η ανακατασκευή δεν κάνουν τίποτα
    # μπαγιάτικο: αν έτρεχαν και οι δύο μέθοδοι, κάθε γραφή GPS θα σήμαινε
    # ολόκληρη τη διαδρομή για επανυπολογισμό χωρίς να αλλάζει τίποτα στο
    # αποτέλεσμα — δηλαδή πλήρης επανυπολογισμός κάθε 15 λεπτά σε αδύναμο VPS.
    m_alias = passage_method_sql("p")
    m_plain = passage_method_sql()

    stale: set[str] = set()

    # 1. never computed
    for r in conn.execute("""
            SELECT r.route_code FROM routes r
            LEFT JOIN daily_route_stats d
                   ON d.route_code = r.route_code AND d.service_date = ?
            WHERE d.route_code IS NULL""", (service_date,)):
        stale.add(r["route_code"])

    # 2. passages recorded after the stored result
    qs, qe = passage_query_window(service_date)
    for r in conn.execute(f"""
            SELECT DISTINCT p.route_code
            FROM stop_passages p
            JOIN daily_route_stats d
              ON d.route_code = p.route_code AND d.service_date = ?
            WHERE p.passed_at >= ? AND p.passed_at < ?
              AND p.recorded_at > d.computed_at {m_alias}""",
            (service_date, qs, qe)):
        stale.add(r["route_code"])

    # 3. own-day schedule changed
    for r in conn.execute("""
            SELECT DISTINCT s.route_code
            FROM scheduled_trips s
            JOIN daily_route_stats d
              ON d.route_code = s.route_code AND d.service_date = ?
            WHERE s.schedule_date = ? AND s.last_synced > d.computed_at""",
            (service_date, service_date)):
        stale.add(r["route_code"])

    # 4. neighbour-day schedule changed AND route active in a boundary zone
    d0 = date.fromisoformat(service_date)
    neighbours = [(d0 - timedelta(days=1)).isoformat(),
                  (d0 + timedelta(days=1)).isoformat()]
    changed_neighbour: set[str] = set()
    for nd in neighbours:
        for r in conn.execute("""
                SELECT DISTINCT s.route_code
                FROM scheduled_trips s
                JOIN daily_route_stats d
                  ON d.route_code = s.route_code AND d.service_date = ?
                WHERE s.schedule_date = ? AND s.last_synced > d.computed_at""",
                (service_date, nd)):
            changed_neighbour.add(r["route_code"])
    if changed_neighbour:
        in_zone: set[str] = set()
        for zs, ze in boundary_zone_windows(service_date):
            for r in conn.execute(
                    "SELECT DISTINCT route_code FROM stop_passages "
                    f"WHERE passed_at >= ? AND passed_at < ? {m_plain}",
                    (zs, ze)):
                in_zone.add(r["route_code"])
        stale |= (changed_neighbour & in_zone)

    known = set(route_codes)
    return [rc for rc in route_codes if rc in stale and rc in known]


def _compute_one_day(service_date: str, computed_at: str, full: bool = False):

    with db.job_run("compute_daily_report") as run:
        conn = db.get_connection()
        try:
            route_rows  = conn.execute("SELECT route_code FROM routes").fetchall()
            all_codes   = [r["route_code"] for r in route_rows]

            # Incremental: recompute only routes whose inputs changed since
            # their stored result. Deterministic ⇒ skipped routes would produce
            # byte-identical output. `full` (explicit date / env override)
            # forces the classic all-routes pass.
            if full:
                route_codes = all_codes
                log.info("Computing report for %s across %d routes (full)",
                         service_date, len(all_codes))
            else:
                route_codes = _routes_needing_recompute(conn, all_codes, service_date)
                log.info("Computing report for %s: %d/%d routes changed "
                         "(%d unchanged, skipped)", service_date,
                         len(route_codes), len(all_codes),
                         len(all_codes) - len(route_codes))

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

            # Chain consistency: an estimate may never contradict a
            # measurement (cross-route, per vehicle). Runs over the WHOLE day
            # and reports which routes it touched, so the incremental pass
            # includes them in slots/stats below.
            try:
                chain = tighten_chain(conn, service_date, computed_at)
                conn.commit()
                extra = [rc for rc in chain["routes"] if rc not in set(route_codes)]
                if extra:
                    route_codes = list(route_codes) + extra
            except Exception as e:
                log.warning("Chain consistency failed (non-fatal): %s", e)

            log.info("Computing rotation slots...")
            slot_stats = compute_all_slots(conn, service_date, computed_at,
                                           route_codes=route_codes)
            conn.commit()

            # Data-quality audit: reports impossible results, changes nothing.
            # Always covers the WHOLE day, even in incremental mode — it is
            # cheap (a handful of aggregate queries) and a violation can span
            # two routes/trips of which only one was recomputed.
            try:
                audit_counts = run_audit(conn, service_date, computed_at)
                conn.commit()
            except Exception as e:
                log.warning("Audit failed (non-fatal): %s", e)
                audit_counts = {}

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
                f"purged_pings={purged['pings']} "
                f"audit_flags={sum(audit_counts.values()) if audit_counts else 0}"
            )
            if errors:
                run.status = "partial"
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
