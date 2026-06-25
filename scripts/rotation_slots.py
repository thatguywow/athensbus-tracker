"""
rotation_slots.py — infers bus rotation patterns and assigns trips to slots.

CONCEPT:
A route with a 10-minute headway and a 40-minute one-way trip time needs
roughly 4 buses in rotation (40/10 = 4 cycles). Bus 1 departs at 06:00,
comes back around 06:40, departs again at 06:50 (next available slot).
Bus 2 departs at 06:10, returns ~06:50, departs again at 07:00. Etc.

We infer this from the schedule and then match observed trips to slots.

SLOT ASSIGNMENT ALGORITHM:
1.  Extract all scheduled departures for the route, sorted by time.
2.  Estimate headway = median gap between consecutive departures.
3.  Estimate cycle time from the route distance and average speed, or
    fall back to: first gap where the same vehicle is seen again.
4.  slot_count = round(cycle_mins / headway_mins), minimum 1.
5.  Assign slot numbers: departure[0] = slot 1, departure[1] = slot 2,
    ..., departure[slot_count] = slot 1 again (next cycle), etc.
6.  For each observed trip, find the nearest scheduled departure within
    a tolerance window, assign it that slot number.
7.  Detect handoffs: if two consecutive trips in the same slot have
    different vehicle numbers, record a slot_handoff.
8.  Build vehicle_activity summary.

DELAY HANDLING:
The matching window widens as the day progresses — a trip that runs
30 minutes late should still match its scheduled slot, not slip to the
next one. We use a rolling window: ±(headway * 0.6) around the scheduled
time, rather than a fixed ±N minutes.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone, date as date_type

import db

log = logging.getLogger("rotation_slots")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _time_to_mins(t: str) -> float:
    """'HH:MM:SS' → float minutes since midnight."""
    h, m, s = t.split(":")
    return int(h) * 60 + int(m) + int(s) / 60


def _iso_to_mins_since_midnight(iso: str, service_date: str) -> float:
    """ISO8601 UTC string → minutes since midnight Athens time on service_date."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    dt = datetime.fromisoformat(iso)
    athens = dt.astimezone(ZoneInfo("Europe/Athens"))
    midnight = datetime(athens.year, athens.month, athens.day,
                        tzinfo=ZoneInfo("Europe/Athens"))
    return (athens - midnight).total_seconds() / 60


# ── Core inference ────────────────────────────────────────────────────────────

def infer_rotation(conn, route_code: str, service_date: str,
                   computed_at: str) -> dict | None:
    """
    Infer the rotation pattern for a route on a given day.
    Returns a summary dict, writes to rotation_patterns table.
    Returns None if there's not enough schedule data.
    """
    sched_rows = conn.execute("""
        SELECT departure_time FROM scheduled_trips
        WHERE route_code = ? AND schedule_date = ?
          AND departure_time IS NOT NULL
        ORDER BY departure_time
    """, (route_code, service_date)).fetchall()

    if len(sched_rows) < 2:
        return None

    times_mins = [_time_to_mins(r["departure_time"]) for r in sched_rows]

    # Headway = median gap between consecutive departures
    gaps = [times_mins[i+1] - times_mins[i]
            for i in range(len(times_mins)-1) if times_mins[i+1] > times_mins[i]]
    if not gaps:
        return None

    headway = statistics.median(gaps)
    if headway < 1:
        return None

    # Estimate cycle time: try to detect it from vehicle return patterns
    # in the actual trip data. Fall back to 2x route distance / avg speed.
    cycle_mins = _estimate_cycle_time(conn, route_code, service_date, headway)

    slot_count = max(1, round(cycle_mins / headway))

    conn.execute("""
        INSERT INTO rotation_patterns
            (route_code, service_date, slot_count, headway_mins, cycle_mins, computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(route_code, service_date) DO UPDATE SET
            slot_count   = excluded.slot_count,
            headway_mins = excluded.headway_mins,
            cycle_mins   = excluded.cycle_mins,
            computed_at  = excluded.computed_at
    """, (route_code, service_date, slot_count,
          round(headway, 2), round(cycle_mins, 2), computed_at))

    return {
        "route_code":   route_code,
        "slot_count":   slot_count,
        "headway_mins": round(headway, 2),
        "cycle_mins":   round(cycle_mins, 2),
    }


def _estimate_cycle_time(conn, route_code: str, service_date: str,
                         headway: float) -> float:
    """
    Try to estimate how long a full cycle (one-way trip + turnaround) takes
    by looking at how long individual trips lasted in the actual GPS data.
    Falls back to headway * 3 if we can't determine it from data.
    """
    trip_rows = conn.execute("""
        SELECT started_at, ended_at FROM trips
        WHERE route_code = ? AND service_date = ?
    """, (route_code, service_date)).fetchall()

    if not trip_rows:
        return headway * 3  # conservative fallback

    durations = []
    for r in trip_rows:
        try:
            start = datetime.fromisoformat(r["started_at"])
            end   = datetime.fromisoformat(r["ended_at"])
            d     = (end - start).total_seconds() / 60
            if 5 < d < 180:   # sanity: ignore <5min (fragments) and >3h outliers
                durations.append(d)
        except Exception:
            pass

    if not durations:
        return headway * 3

    # Cycle = trip time × 2 (out + back) + estimated turnaround (~5 min)
    avg_trip = statistics.median(durations)
    return avg_trip * 2 + 5


# ── Slot assignment ───────────────────────────────────────────────────────────

def assign_slots(conn, route_code: str, service_date: str,
                 computed_at: str) -> dict:
    """
    Assign each observed trip to a rotation slot.
    Writes to slot_assignments, slot_handoffs, vehicle_activity.
    """
    pattern = conn.execute("""
        SELECT slot_count, headway_mins, cycle_mins
        FROM rotation_patterns
        WHERE route_code = ? AND service_date = ?
    """, (route_code, service_date)).fetchone()

    if not pattern:
        return {"assigned": 0, "handoffs": 0}

    slot_count  = pattern["slot_count"]
    headway     = pattern["headway_mins"]
    match_window = headway * 0.6   # ±60% of headway to absorb delays

    # Scheduled departures in order
    sched_rows = conn.execute("""
        SELECT id, departure_time FROM scheduled_trips
        WHERE route_code = ? AND schedule_date = ?
          AND departure_time IS NOT NULL
        ORDER BY departure_time
    """, (route_code, service_date)).fetchall()

    if not sched_rows:
        return {"assigned": 0, "handoffs": 0}

    # Build slot map: scheduled_departure_index → slot_number (1-based)
    sched_slot = {}
    for i, row in enumerate(sched_rows):
        sched_slot[row["id"]] = (i % slot_count) + 1

    # Actual trips sorted by departure time
    trip_rows = conn.execute("""
        SELECT id, vehicle_no, started_at, ended_at FROM trips
        WHERE route_code = ? AND service_date = ?
        ORDER BY started_at
    """, (route_code, service_date)).fetchall()

    # For each trip, find the best matching scheduled departure
    n_assigned  = 0
    n_handoffs  = 0

    # Track last vehicle per slot for handoff detection
    last_vehicle_per_slot: dict[int, tuple[str, str]] = {}
    # (vehicle_no, ended_at)

    for trip in trip_rows:
        trip_mins = _iso_to_mins_since_midnight(trip["started_at"], service_date)

        best_sched_id  = None
        best_sched_time = None
        best_diff       = float("inf")

        for sr in sched_rows:
            sched_mins = _time_to_mins(sr["departure_time"])
            # Allow trips up to 3× headway late (bad day) but not early
            diff = trip_mins - sched_mins
            if -match_window <= diff <= headway * 3:
                if abs(diff) < abs(best_diff):
                    best_diff      = diff
                    best_sched_id  = sr["id"]
                    best_sched_time = sr["departure_time"]

        if best_sched_id is None:
            continue  # trip doesn't match any scheduled slot (extra/ghost run)

        slot_num = sched_slot[best_sched_id]
        deviation = round(best_diff, 1)

        conn.execute("""
            INSERT INTO slot_assignments
                (trip_id, route_code, service_date, slot_number,
                 scheduled_departure, departure_deviation_mins, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trip_id) DO UPDATE SET
                slot_number             = excluded.slot_number,
                scheduled_departure     = excluded.scheduled_departure,
                departure_deviation_mins = excluded.departure_deviation_mins,
                computed_at             = excluded.computed_at
        """, (trip["id"], route_code, service_date, slot_num,
              best_sched_time, deviation, computed_at))
        n_assigned += 1

        # Handoff detection: did a different vehicle take over this slot?
        if slot_num in last_vehicle_per_slot:
            prev_veh, prev_ended = last_vehicle_per_slot[slot_num]
            if prev_veh != trip["vehicle_no"]:
                try:
                    gap = (
                        datetime.fromisoformat(trip["started_at"]) -
                        datetime.fromisoformat(prev_ended)
                    ).total_seconds() / 60
                except Exception:
                    gap = None

                conn.execute("""
                    INSERT INTO slot_handoffs
                        (route_code, service_date, slot_number,
                         outgoing_vehicle, incoming_vehicle,
                         handoff_time, gap_mins, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (route_code, service_date, slot_num,
                      prev_veh, trip["vehicle_no"],
                      trip["started_at"], round(gap, 1) if gap else None,
                      computed_at))
                n_handoffs += 1

        last_vehicle_per_slot[slot_num] = (trip["vehicle_no"], trip["ended_at"])

    return {"assigned": n_assigned, "handoffs": n_handoffs}


# ── Vehicle activity summary ──────────────────────────────────────────────────

def build_vehicle_activity(conn, service_date: str, computed_at: str):
    """
    Aggregate slot_assignments + trips → vehicle_activity rows.
    One row per (vehicle, service_date, route, slot).
    """
    # Clear and rebuild for this date (idempotent)
    conn.execute(
        "DELETE FROM vehicle_activity WHERE service_date = ?", (service_date,)
    )

    rows = conn.execute("""
        SELECT t.vehicle_no, t.route_code, sa.slot_number,
               COUNT(*)              AS trip_count,
               MIN(t.started_at)    AS first_dep,
               MAX(t.started_at)    AS last_dep,
               SUM(
                 (strftime('%s', t.ended_at) - strftime('%s', t.started_at)) / 60.0
               )                    AS total_mins
        FROM trips t
        JOIN slot_assignments sa ON sa.trip_id = t.id
        WHERE t.service_date = ?
        GROUP BY t.vehicle_no, t.route_code, sa.slot_number
        ORDER BY t.vehicle_no, t.route_code, sa.slot_number
    """, (service_date,)).fetchall()

    for r in rows:
        conn.execute("""
            INSERT INTO vehicle_activity
                (vehicle_no, service_date, route_code, slot_number,
                 trip_count, first_departure, last_departure, total_mins, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_no, service_date, route_code, slot_number)
            DO UPDATE SET
                trip_count      = excluded.trip_count,
                first_departure = excluded.first_departure,
                last_departure  = excluded.last_departure,
                total_mins      = excluded.total_mins,
                computed_at     = excluded.computed_at
        """, (r["vehicle_no"], service_date, r["route_code"], r["slot_number"],
              r["trip_count"], r["first_dep"], r["last_dep"],
              round(r["total_mins"] or 0, 1), computed_at))


# ── Main entry point (called from compute_daily_report) ──────────────────────

def compute_all_slots(conn, service_date: str, computed_at: str) -> dict:
    """Run full slot inference + assignment for all routes on service_date."""
    routes = conn.execute("SELECT route_code FROM routes").fetchall()

    n_patterns = 0
    n_assigned = 0
    n_handoffs = 0

    for r in routes:
        rc = r["route_code"]
        try:
            pat = infer_rotation(conn, rc, service_date, computed_at)
            if pat:
                n_patterns += 1
            result = assign_slots(conn, rc, service_date, computed_at)
            n_assigned += result["assigned"]
            n_handoffs += result["handoffs"]
        except Exception as e:
            log.warning("Slot computation failed for route %s: %s", rc, e)

    build_vehicle_activity(conn, service_date, computed_at)

    return {
        "patterns": n_patterns,
        "assigned": n_assigned,
        "handoffs": n_handoffs,
    }
