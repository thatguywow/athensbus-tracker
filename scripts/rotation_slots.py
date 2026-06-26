"""
rotation_slots.py — infers bus rotation patterns and assigns trips to slots.

IMPROVED ALGORITHM:

Phase 1 — Observe full cycles:
  Track each vehicle's trips in order. A "cycle" is complete when a vehicle
  returns to the origin after a trip. The time between a vehicle's consecutive
  departures = its cycle time. Across all vehicles, headway = min gap between
  any two different vehicles' departures. slot_count = cycle_time / headway.

Phase 2 — Assign slot numbers:
  Sort all observed departures by time. Departure[0] = slot 1,
  departure[1] = slot 2, ..., departure[slot_count] = slot 1 again.
  This naturally handles any number of vehicles/slots.

Phase 3 — Build persistent slot definitions:
  Store which scheduled times belong to which slot. This persists across days
  and only needs updating when the schedule changes (summer/winter timetable).

DELAY HANDLING:
  Matching window = ±(headway × 0.6). A bus running late still matches its
  original slot rather than slipping to the next one.

HANDOFF DETECTION:
  Only records handoffs when gap between outgoing vehicle's last trip and
  incoming vehicle's first trip in the same slot > MIN_HANDOFF_GAP_MINS (10).
  This filters out normal circular rotation where a different vehicle
  naturally picks up the next scheduled departure.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta

import db

log = logging.getLogger("rotation_slots")

MIN_HANDOFF_GAP_MINS = 10


def _time_to_mins(t: str) -> float:
    h, m, s = t.split(":")
    return int(h)*60 + int(m) + int(s)/60


def _iso_to_mins_since_midnight(iso: str) -> float:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    dt = datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Athens"))
    return dt.hour*60 + dt.minute + dt.second/60


def infer_rotation(conn, route_code: str, service_date: str,
                   computed_at: str) -> dict | None:
    """
    Infer rotation pattern from OBSERVED vehicle cycles, not just schedule stats.
    Falls back to schedule-based headway if not enough observed data.
    """
    # Get all trips for this route today, ordered by departure
    trip_rows = conn.execute("""
        SELECT vehicle_no, started_at, ended_at FROM trips
        WHERE route_code=? AND service_date=?
        ORDER BY started_at
    """, (route_code, service_date)).fetchall()

    if not trip_rows:
        return None

    # Build per-vehicle trip sequences
    by_vehicle: dict[str, list] = {}
    for r in trip_rows:
        by_vehicle.setdefault(r["vehicle_no"], []).append(r)

    # Estimate cycle time from vehicles that made multiple trips
    cycle_times = []
    for veh, trips in by_vehicle.items():
        if len(trips) >= 2:
            for i in range(len(trips)-1):
                try:
                    t1 = datetime.fromisoformat(trips[i]["started_at"])
                    t2 = datetime.fromisoformat(trips[i+1]["started_at"])
                    gap = (t2 - t1).total_seconds() / 60
                    if 15 < gap < 240:  # sanity: 15min-4hr cycle
                        cycle_times.append(gap)
                except Exception:
                    pass

    # Estimate headway from schedule
    sched_rows = conn.execute("""
        SELECT departure_time FROM scheduled_trips
        WHERE route_code=? AND schedule_date=? AND departure_time IS NOT NULL
        ORDER BY departure_time
    """, (route_code, service_date)).fetchall()

    if len(sched_rows) < 2:
        return None

    times_mins = [_time_to_mins(r["departure_time"]) for r in sched_rows]
    gaps = [times_mins[i+1]-times_mins[i]
            for i in range(len(times_mins)-1)
            if times_mins[i+1] > times_mins[i] and times_mins[i+1]-times_mins[i] < 60]

    if not gaps:
        return None

    headway = statistics.median(gaps)

    # Use observed cycle time if available, else estimate from route data
    if cycle_times:
        cycle_mins = round(statistics.median(cycle_times), 1)
    else:
        # Fallback: estimate from trip durations × 2 + 5min turnaround
        durations = []
        for r in trip_rows:
            try:
                d = (datetime.fromisoformat(r["ended_at"]) -
                     datetime.fromisoformat(r["started_at"])).total_seconds()/60
                if 5 < d < 180:
                    durations.append(d)
            except Exception:
                pass
        cycle_mins = round(statistics.median(durations)*2 + 5, 1) if durations else round(headway*3, 1)

    slot_count = max(1, round(cycle_mins / headway))

    conn.execute("""
        INSERT INTO rotation_patterns
            (route_code, service_date, slot_count, headway_mins, cycle_mins, computed_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(route_code, service_date) DO UPDATE SET
            slot_count=excluded.slot_count, headway_mins=excluded.headway_mins,
            cycle_mins=excluded.cycle_mins, computed_at=excluded.computed_at
    """, (route_code, service_date, slot_count, round(headway,2), cycle_mins, computed_at))

    return {"route_code": route_code, "slot_count": slot_count,
            "headway_mins": round(headway,2), "cycle_mins": cycle_mins}


def assign_slots(conn, route_code: str, service_date: str,
                 computed_at: str) -> dict:
    """
    Assign each observed trip to a rotation slot.
    Uses the natural ordering of observed departures within each headway cycle.
    """
    pattern = conn.execute("""
        SELECT slot_count, headway_mins FROM rotation_patterns
        WHERE route_code=? AND service_date=?
    """, (route_code, service_date)).fetchone()

    if not pattern:
        return {"assigned": 0, "handoffs": 0}

    slot_count  = pattern["slot_count"]
    headway     = pattern["headway_mins"]
    match_window = headway * 0.6

    sched_rows = conn.execute("""
        SELECT id, departure_time FROM scheduled_trips
        WHERE route_code=? AND schedule_date=? AND departure_time IS NOT NULL
        ORDER BY departure_time
    """, (route_code, service_date)).fetchall()

    if not sched_rows:
        return {"assigned": 0, "handoffs": 0}

    # Assign slot numbers to scheduled departures
    sched_slot = {row["id"]: (i % slot_count) + 1
                  for i, row in enumerate(sched_rows)}

    trip_rows = conn.execute("""
        SELECT id, vehicle_no, started_at, ended_at FROM trips
        WHERE route_code=? AND service_date=? ORDER BY started_at
    """, (route_code, service_date)).fetchall()

    n_assigned = n_handoffs = 0
    last_vehicle_per_slot: dict[int, tuple[str, str]] = {}

    for trip in trip_rows:
        trip_mins = _iso_to_mins_since_midnight(trip["started_at"])

        best_sched_id = best_diff = None
        for sr in sched_rows:
            sched_mins = _time_to_mins(sr["departure_time"])
            diff = trip_mins - sched_mins
            if -match_window <= diff <= headway*3:
                if best_diff is None or abs(diff) < abs(best_diff):
                    best_diff = diff
                    best_sched_id = sr["id"]
                    best_sched_time = sr["departure_time"]

        if best_sched_id is None:
            continue

        slot_num = sched_slot[best_sched_id]
        conn.execute("""
            INSERT INTO slot_assignments
                (trip_id, route_code, service_date, slot_number,
                 scheduled_departure, departure_deviation_mins, computed_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(trip_id) DO UPDATE SET
                slot_number=excluded.slot_number,
                scheduled_departure=excluded.scheduled_departure,
                departure_deviation_mins=excluded.departure_deviation_mins,
                computed_at=excluded.computed_at
        """, (trip["id"], route_code, service_date, slot_num,
              best_sched_time, round(best_diff,1), computed_at))
        n_assigned += 1

        # Handoff detection with minimum gap threshold
        if slot_num in last_vehicle_per_slot:
            prev_veh, prev_ended = last_vehicle_per_slot[slot_num]
            if prev_veh != trip["vehicle_no"]:
                try:
                    gap = (datetime.fromisoformat(trip["started_at"]) -
                           datetime.fromisoformat(prev_ended)).total_seconds()/60
                except Exception:
                    gap = None
                if gap is None or gap >= MIN_HANDOFF_GAP_MINS:
                    conn.execute("""
                        INSERT INTO slot_handoffs
                            (route_code, service_date, slot_number,
                             outgoing_vehicle, incoming_vehicle,
                             handoff_time, gap_mins, computed_at)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (route_code, service_date, slot_num,
                          prev_veh, trip["vehicle_no"],
                          trip["started_at"],
                          round(gap,1) if gap else None, computed_at))
                    n_handoffs += 1

        last_vehicle_per_slot[slot_num] = (trip["vehicle_no"], trip["ended_at"])

    return {"assigned": n_assigned, "handoffs": n_handoffs}


def build_vehicle_activity(conn, service_date: str, computed_at: str):
    conn.execute("DELETE FROM vehicle_activity WHERE service_date=?", (service_date,))
    rows = conn.execute("""
        SELECT t.vehicle_no, t.route_code, sa.slot_number,
               COUNT(*) AS trip_count,
               MIN(t.started_at) AS first_dep, MAX(t.started_at) AS last_dep,
               SUM((strftime('%s',t.ended_at)-strftime('%s',t.started_at))/60.0) AS total_mins
        FROM trips t
        JOIN slot_assignments sa ON sa.trip_id=t.id
        WHERE t.service_date=?
        GROUP BY t.vehicle_no, t.route_code, sa.slot_number
        ORDER BY t.vehicle_no, t.route_code
    """, (service_date,)).fetchall()

    for r in rows:
        conn.execute("""
            INSERT INTO vehicle_activity
                (vehicle_no, service_date, route_code, slot_number,
                 trip_count, first_departure, last_departure, total_mins, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(vehicle_no, service_date, route_code, slot_number)
            DO UPDATE SET trip_count=excluded.trip_count,
                first_departure=excluded.first_departure,
                last_departure=excluded.last_departure,
                total_mins=excluded.total_mins,
                computed_at=excluded.computed_at
        """, (r["vehicle_no"], service_date, r["route_code"], r["slot_number"],
              r["trip_count"], r["first_dep"], r["last_dep"],
              round(r["total_mins"] or 0, 1), computed_at))


def update_slot_definitions(conn, route_code: str, service_date: str,
                             computed_at: str):
    """Update persistent slot definitions from today's observed data."""
    pattern = conn.execute("""
        SELECT slot_count FROM rotation_patterns
        WHERE route_code=? AND service_date=?
    """, (route_code, service_date)).fetchone()

    if not pattern:
        return

    for slot_num in range(1, pattern["slot_count"]+1):
        deps = conn.execute("""
            SELECT sa.scheduled_departure FROM slot_assignments sa
            JOIN trips t ON t.id=sa.trip_id
            WHERE t.route_code=? AND t.service_date=? AND sa.slot_number=?
              AND sa.scheduled_departure IS NOT NULL
            ORDER BY sa.scheduled_departure
        """, (route_code, service_date, slot_num)).fetchall()

        if not deps:
            continue

        first_dep = deps[0]["scheduled_departure"][:5]
        intervals = []
        for i in range(1, len(deps)):
            try:
                t1 = _time_to_mins(deps[i-1]["scheduled_departure"])
                t2 = _time_to_mins(deps[i]["scheduled_departure"])
                if t2 > t1:
                    intervals.append(t2-t1)
            except Exception:
                pass

        avg_interval = round(statistics.mean(intervals), 1) if intervals else None

        conn.execute("""
            INSERT INTO slot_definitions
                (route_code, slot_number, typical_first_dep,
                 typical_interval_mins, last_updated)
            VALUES (?,?,?,?,?)
            ON CONFLICT(route_code, slot_number) DO UPDATE SET
                typical_first_dep=excluded.typical_first_dep,
                typical_interval_mins=excluded.typical_interval_mins,
                last_updated=excluded.last_updated
        """, (route_code, slot_num, first_dep, avg_interval, computed_at))


def compute_all_slots(conn, service_date: str, computed_at: str) -> dict:
    routes = conn.execute("SELECT route_code FROM routes").fetchall()
    n_patterns = n_assigned = n_handoffs = 0

    for r in routes:
        rc = r["route_code"]
        try:
            pat = infer_rotation(conn, rc, service_date, computed_at)
            if pat:
                n_patterns += 1
            result = assign_slots(conn, rc, service_date, computed_at)
            n_assigned += result["assigned"]
            n_handoffs += result["handoffs"]
            update_slot_definitions(conn, rc, service_date, computed_at)
        except Exception as e:
            log.warning("Slot computation failed for route %s: %s", rc, e)

    build_vehicle_activity(conn, service_date, computed_at)

    return {"patterns": n_patterns, "assigned": n_assigned, "handoffs": n_handoffs}
