"""
audit_day.py — data-quality auditor.

Runs after each compute and counts violations of things that CANNOT happen in
the physical world (a bus in two places at once, a trip departing before the
same bus finished the previous one, a lap done in a fraction of the usual time,
one schedule slot covered twice). It never changes the computed data — it only
reports, so a human sees the top offenders instead of hunting through tables.

Storage is bounded on purpose:
  • audit_summary   → one row per (day, type): the count. Tiny, always written.
  • audit_findings  → concrete examples, capped at MAX_EXAMPLES per type/day.
Both are purged with the usual retention window.

Deliberately NOT flagged: ordinary lateness, long dwell times, low execution
rate. Those are how the network actually behaves, not errors.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MAX_EXAMPLES = 50          # per finding type per day
FAST_FRACTION = 0.5        # duration < 50% of the route median ⇒ suspicious
SLOW_FRACTION = 2.0        # duration > 200% of the route median ⇒ suspicious
MIN_MEDIAN_MINS = 10.0     # ignore routes without a usable median

TYPES = (
    "vehicle_overlap",       # same vehicle, two trips overlapping in time
    "departure_inversion",   # departs before its own previous trip arrived
    "duration_too_short",    # implausibly fast lap
    "duration_too_long",     # implausibly slow lap
    "slot_double_cover",     # one scheduled slot matched by two trips
    "no_departure_observed", # trip whose start was never seen (partial trip)
)


def _record(conn, service_date, ftype, computed_at, rows):
    """Store the count plus up to MAX_EXAMPLES concrete examples."""
    conn.execute("""
        INSERT INTO audit_summary (service_date, finding_type, count, computed_at)
        VALUES (?,?,?,?)
        ON CONFLICT(service_date, finding_type) DO UPDATE SET
            count = excluded.count, computed_at = excluded.computed_at
    """, (service_date, ftype, len(rows), computed_at))

    conn.execute("DELETE FROM audit_findings WHERE service_date=? AND finding_type=?",
                 (service_date, ftype))
    for r in rows[:MAX_EXAMPLES]:
        conn.execute("""
            INSERT OR IGNORE INTO audit_findings
                (service_date, finding_type, route_code, line_id,
                 vehicle_no, trip_id, detail, computed_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (service_date, ftype, r.get("route_code"), r.get("line_id"),
              r.get("vehicle_no"), r.get("trip_id"), r.get("detail"), computed_at))
    return len(rows)


def _rows(conn, sql, params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def run_audit(conn, service_date: str, computed_at: str) -> dict:
    """Audit one service day. Read-only w.r.t. computed data."""
    counts: dict[str, int] = {}

    # ── 1/2. same vehicle in two places at once, and departure inversions ──
    pairs = _rows(conn, """
        SELECT a.id a_id, b.id b_id, a.route_code, l.line_id, a.vehicle_no,
               a.started_at a_dep, a.terminus_arrived_at a_arr,
               b.started_at b_dep, b.terminus_arrived_at b_arr
        FROM trips a
        JOIN trips b ON b.vehicle_no = a.vehicle_no
                    AND b.service_date = a.service_date
                    AND b.started_at > a.started_at
        JOIN routes r ON r.route_code = a.route_code
        JOIN lines  l ON l.line_code  = r.line_code
        WHERE a.service_date = ?
          AND a.terminus_arrived_at IS NOT NULL
          AND b.started_at < a.terminus_arrived_at
        ORDER BY a.started_at
    """, (service_date,))

    overlaps, inversions = [], []
    for p in pairs:
        rec = {"route_code": p["route_code"], "line_id": p["line_id"],
               "vehicle_no": p["vehicle_no"], "trip_id": p["b_id"],
               "detail": (f"prev {p['a_dep'][11:16]}→{p['a_arr'][11:16]} "
                          f"vs next {p['b_dep'][11:16]}")}
        # Overlap proper: the second trip is still running when the first ends.
        if p["b_arr"] and p["b_arr"] > p["a_arr"]:
            overlaps.append(rec)
        else:
            inversions.append(rec)
    counts["vehicle_overlap"] = _record(conn, service_date, "vehicle_overlap",
                                       computed_at, overlaps)
    counts["departure_inversion"] = _record(conn, service_date,
                                           "departure_inversion",
                                           computed_at, inversions)

    # ── 3/4. implausible durations against the route's own median ──
    dur = _rows(conn, """
        SELECT t.id trip_id, t.route_code, l.line_id, t.vehicle_no,
               t.started_at, t.terminus_arrived_at,
               rr.median_trip_duration_mins med,
               (julianday(t.terminus_arrived_at) - julianday(t.started_at)) * 1440 mins
        FROM trips t
        JOIN routes r ON r.route_code = t.route_code
        JOIN lines  l ON l.line_code  = r.line_code
        JOIN route_rotation rr ON rr.route_code = t.route_code
        WHERE t.service_date = ?
          AND t.terminus_arrived_at IS NOT NULL
          AND rr.median_trip_duration_mins >= ?
    """, (service_date, MIN_MEDIAN_MINS))

    short, long_ = [], []
    for d in dur:
        rec = {"route_code": d["route_code"], "line_id": d["line_id"],
               "vehicle_no": d["vehicle_no"], "trip_id": d["trip_id"],
               "detail": (f"{d['started_at'][11:16]} διάρκεια "
                          f"{d['mins']:.0f}′ vs τυπικό {d['med']:.0f}′")}
        if d["mins"] < d["med"] * FAST_FRACTION:
            short.append(rec)
        elif d["mins"] > d["med"] * SLOW_FRACTION:
            long_.append(rec)
    counts["duration_too_short"] = _record(conn, service_date,
                                           "duration_too_short", computed_at, short)
    counts["duration_too_long"] = _record(conn, service_date,
                                          "duration_too_long", computed_at, long_)

    # ── 5. one scheduled slot claimed by two trips ──
    dbl = _rows(conn, """
        SELECT sa.route_code, l.line_id, sa.scheduled_departure,
               COUNT(*) n, MIN(sa.trip_id) trip_id
        FROM slot_assignments sa
        JOIN routes r ON r.route_code = sa.route_code
        JOIN lines  l ON l.line_code  = r.line_code
        WHERE sa.service_date = ? AND sa.scheduled_departure IS NOT NULL
        GROUP BY sa.route_code, sa.scheduled_departure
        HAVING COUNT(*) > 1
    """, (service_date,))
    counts["slot_double_cover"] = _record(
        conn, service_date, "slot_double_cover", computed_at,
        [{"route_code": d["route_code"], "line_id": d["line_id"],
          "trip_id": d["trip_id"], "vehicle_no": None,
          "detail": f"slot {d['scheduled_departure'][:5]} × {d['n']}"} for d in dbl])

    # ── 6. trips whose departure was never observed (partial trips) ──
    partial = _rows(conn, """
        SELECT t.id trip_id, t.route_code, l.line_id, t.vehicle_no, t.started_at
        FROM trips t
        JOIN routes r ON r.route_code = t.route_code
        JOIN lines  l ON l.line_code  = r.line_code
        WHERE t.service_date = ?
          AND NOT EXISTS (
              SELECT 1 FROM trip_stop_times x
              WHERE x.trip_id = t.id
                AND x.stop_order <= (SELECT (MIN(stop_order)+MAX(stop_order))/2.0
                                     FROM stops s WHERE s.route_code = t.route_code))
    """, (service_date,))
    counts["no_departure_observed"] = _record(
        conn, service_date, "no_departure_observed", computed_at,
        [{"route_code": p["route_code"], "line_id": p["line_id"],
          "vehicle_no": p["vehicle_no"], "trip_id": p["trip_id"],
          "detail": f"πρώτη θέαση {p['started_at'][11:16]}"} for p in partial])

    total = sum(counts.values())
    log.info("Audit %s: %s (σύνολο %d)",
             service_date,
             " ".join(f"{k}={v}" for k, v in counts.items() if v), total)
    return counts


def purge_audit(conn, cutoff_date: str) -> int:
    """Drop audit rows for days older than cutoff_date (YYYY-MM-DD)."""
    n = conn.execute("DELETE FROM audit_findings WHERE service_date < ?",
                     (cutoff_date,)).rowcount
    conn.execute("DELETE FROM audit_summary WHERE service_date < ?", (cutoff_date,))
    return n
