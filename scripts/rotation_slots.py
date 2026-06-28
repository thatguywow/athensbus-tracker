"""
rotation_slots.py — infers bus rotation patterns (καρτελάκια) and assigns
trips to slots, with maximum accuracy.

METHODOLOGY
===========

A "καρτελάκι" (slot) is a POSITION in the rotation sequence, independent of
which physical vehicle fills it. For a line with N active vehicles there are
N slots. The Nth scheduled departure of the day belongs to slot
((N-1) mod slot_count) + 1. This pattern is STABLE day-to-day; only the
vehicle number filling each slot changes.

1. SLOT COUNT (persistent, accumulated across days)
   slot_count = round(cycle_time / headway)
   - headway: median gap between consecutive scheduled departures
   - cycle_time: median time for ONE vehicle to return for its next departure,
     measured from same-vehicle consecutive departures, ACCUMULATED across days
     in the route_rotation table. More days → more samples → higher accuracy.
   - confidence_days tracks how many days confirmed the count; it locks in.

2. SLOT GRID (stable, ordinal — delay-immune)
   Each scheduled departure (sorted by time) gets slot ((i) mod slot_count)+1.
   Because this is based on the theoretical schedule POSITION, not actual
   arrival times, it is immune to delays and missing vehicles. A missing bus
   simply leaves its slot unfilled that day; the pattern is unchanged.

3. TRIP → SLOT ASSIGNMENT (order-preserving DP alignment)
   Actual trips are matched to scheduled departures using a dynamic-programming
   sequence alignment that PRESERVES ORDER and minimises total time deviation,
   allowing scheduled slots to be skipped (missing bus). This correctly handles
   the dangerous case of a uniformly-late set of buses without reordering them,
   unlike independent nearest-time matching.

4. HANDOFFS
   Recorded only when a slot's vehicle changes with a gap > MIN_HANDOFF_GAP_MINS,
   filtering normal circular rotation from real shift changes.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta

import db

log = logging.getLogger("rotation_slots")

MIN_HANDOFF_GAP_MINS = 10
MAX_CYCLE_SAMPLES    = 200   # rolling window of cycle observations

# Asymmetric matching: buses rarely depart EARLY (≤ a few min) but often LATE.
# So matching an observed departure to an earlier scheduled slot (= bus is late)
# is far more plausible than to a later slot (= bus left early).
EARLY_GRACE_MINS   = 5     # buses may leave up to ~5 min early without penalty
MAX_EARLY_MINS     = 9     # beyond this early, almost never a real match
MAX_LATE_MINS      = 40    # buses can run quite late
EARLY_PENALTY      = 4.0   # cost multiplier for leaving early beyond grace


def match_cost(actual_mins: float, scheduled_mins: float) -> float:
    """
    Asymmetric cost of matching an observed departure to a scheduled one.
    deviation > 0 = bus left late (plausible, low cost)
    deviation < 0 = bus left early (implausible beyond a few min, high cost)
    Returns float('inf') if the match is outside acceptable bounds.
    """
    dev = actual_mins - scheduled_mins
    if dev >= 0:                       # late
        return float("inf") if dev > MAX_LATE_MINS else dev
    early = -dev                        # how many minutes early
    if early > MAX_EARLY_MINS:
        return float("inf")
    if early <= EARLY_GRACE_MINS:
        return early                    # small grace, treated normally
    return EARLY_GRACE_MINS + (early - EARLY_GRACE_MINS) * EARLY_PENALTY


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


# ── Phase 1: slot count (persistent, accumulated) ────────────────────────────

def observe_cycle_times(conn, route_code: str, service_date: str) -> list[float]:
    """Measure cycle time from same-vehicle consecutive departures today."""
    trip_rows = conn.execute("""
        SELECT vehicle_no, started_at FROM trips
        WHERE route_code=? AND service_date=?
        ORDER BY vehicle_no, started_at
    """, (route_code, service_date)).fetchall()

    by_vehicle: dict[str, list] = {}
    for r in trip_rows:
        by_vehicle.setdefault(r["vehicle_no"], []).append(r["started_at"])

    cycles = []
    for veh, deps in by_vehicle.items():
        for i in range(len(deps)-1):
            try:
                gap = (datetime.fromisoformat(deps[i+1]) -
                       datetime.fromisoformat(deps[i])).total_seconds()/60
                if 15 < gap < 240:   # sane cycle bounds
                    cycles.append(round(gap, 1))
            except Exception:
                pass
    return cycles


def measure_headway(conn, route_code: str, service_date: str) -> float | None:
    sched_rows = conn.execute("""
        SELECT departure_time FROM scheduled_trips
        WHERE route_code=? AND schedule_date=? AND departure_time IS NOT NULL
        ORDER BY departure_time
    """, (route_code, service_date)).fetchall()
    if len(sched_rows) < 2:
        return None
    times = [_time_to_mins(r["departure_time"]) for r in sched_rows]
    gaps = [times[i+1]-times[i] for i in range(len(times)-1)
            if 0 < times[i+1]-times[i] < 60]
    return statistics.median(gaps) if gaps else None


def _accumulate_segment_times(conn, route_code, service_date, computed_at):
    """
    For each vehicle that passed the ORIGIN (stop_order = min) and one or more
    near-origin stops on the same trip, record the origin→stop travel time.
    Persists the median per (route, stop_order) in segment_times.
    """
    rows = conn.execute("""
        SELECT vehicle_no, stop_order, passed_at, stop_type
        FROM stop_passages
        WHERE route_code=? AND service_date=?
        ORDER BY vehicle_no, passed_at
    """, (route_code, service_date)).fetchall()
    if not rows:
        return

    bounds = conn.execute(
        "SELECT MIN(stop_order) lo FROM stops WHERE route_code=?", (route_code,)
    ).fetchone()
    lo = bounds["lo"] if bounds else None
    if lo is None:
        return

    # Group by vehicle, find origin passage then subsequent near stops within 25min
    by_veh = {}
    for r in rows:
        by_veh.setdefault(r["vehicle_no"], []).append(r)

    new_samples = {}  # stop_order → [mins,...]
    for veh, ps in by_veh.items():
        origin_p = next((p for p in ps if p["stop_order"] == lo), None)
        if not origin_p:
            continue
        t0 = datetime.fromisoformat(origin_p["passed_at"])
        for p in ps:
            if p["stop_order"] <= lo:
                continue
            dt_min = (datetime.fromisoformat(p["passed_at"]) - t0).total_seconds()/60
            if 0 < dt_min < 25:   # plausible near-origin segment
                new_samples.setdefault(p["stop_order"], []).append(round(dt_min, 2))

    for stop_order, samples in new_samples.items():
        row = conn.execute(
            "SELECT samples FROM segment_times WHERE route_code=? AND stop_order=?",
            (route_code, stop_order)).fetchone()
        existing = json.loads(row["samples"]) if (row and row["samples"]) else []
        existing.extend(samples)
        existing = existing[-MAX_CYCLE_SAMPLES:]
        median_mins = round(statistics.median(existing), 2) if existing else None
        conn.execute("""
            INSERT INTO segment_times (route_code, stop_order, median_mins, samples, last_updated)
            VALUES (?,?,?,?,?)
            ON CONFLICT(route_code, stop_order) DO UPDATE SET
                median_mins=excluded.median_mins,
                samples=excluded.samples,
                last_updated=excluded.last_updated
        """, (route_code, stop_order, median_mins, json.dumps(existing), computed_at))


def update_route_rotation(conn, route_code: str, service_date: str,
                          computed_at: str) -> dict | None:
    """
    Update the persistent route_rotation record with today's observations,
    returning the current best estimate of slot_count, headway, cycle.
    """
    headway = measure_headway(conn, route_code, service_date)
    if headway is None or headway <= 0:
        return None

    today_cycles = observe_cycle_times(conn, route_code, service_date)

    row = conn.execute(
        "SELECT * FROM route_rotation WHERE route_code=?", (route_code,)
    ).fetchone()

    if row:
        samples = json.loads(row["cycle_samples"] or "[]")
    else:
        samples = []

    samples.extend(today_cycles)
    samples = samples[-MAX_CYCLE_SAMPLES:]   # keep rolling window

    # Cycle estimate: median of accumulated samples, or fallback from durations
    if samples:
        cycle_mins = round(statistics.median(samples), 1)
    elif row and row["median_cycle_mins"]:
        cycle_mins = row["median_cycle_mins"]
    else:
        # Fallback: median trip duration × 2 + 5min turnaround
        durs = conn.execute("""
            SELECT (strftime('%s',ended_at)-strftime('%s',started_at))/60.0 d
            FROM trips WHERE route_code=? AND service_date=?
              AND ended_at IS NOT NULL
        """, (route_code, service_date)).fetchall()
        dvals = [r["d"] for r in durs if r["d"] and 5 < r["d"] < 180]
        cycle_mins = round(statistics.median(dvals)*2 + 5, 1) if dvals else headway*3

    slot_count = max(1, round(cycle_mins / headway))

    # ── Accumulate per-segment travel times (origin → near-origin stops) ──
    # When a vehicle was seen passing the ORIGIN and also a near-origin stop on
    # the same trip, the time difference is the real segment time. Median across
    # days gives an accurate offset for departure back-calculation.
    try:
        _accumulate_segment_times(conn, route_code, service_date, computed_at)
    except Exception:
        pass

    # ── Accumulate route trip duration (origin→terminus) for departure
    #    extrapolation on sparse days ──
    dur_rows = conn.execute("""
        SELECT (strftime('%s',terminus_arrived_at)-strftime('%s',started_at))/60.0 d
        FROM trips WHERE route_code=? AND service_date=?
          AND terminus_arrived_at IS NOT NULL
    """, (route_code, service_date)).fetchall()
    today_durs = [round(r["d"],1) for r in dur_rows if r["d"] and 5 < r["d"] < 180]

    dur_samples = json.loads(row["duration_samples"]) if (row and row["duration_samples"]) else []
    dur_samples.extend(today_durs)
    dur_samples = dur_samples[-MAX_CYCLE_SAMPLES:]
    if dur_samples:
        median_duration = round(statistics.median(dur_samples), 1)
    elif row and row["median_trip_duration_mins"]:
        median_duration = row["median_trip_duration_mins"]
    else:
        median_duration = None

    # Confidence: increment if today's count agrees with stored count
    if row and row["slot_count"] == slot_count:
        confidence = (row["confidence_days"] or 1) + 1
    else:
        confidence = 1

    conn.execute("""
        INSERT INTO route_rotation
            (route_code, slot_count, median_cycle_mins, median_headway_mins,
             median_trip_duration_mins, duration_samples,
             confidence_days, cycle_samples, last_updated)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(route_code) DO UPDATE SET
            slot_count=excluded.slot_count,
            median_cycle_mins=excluded.median_cycle_mins,
            median_headway_mins=excluded.median_headway_mins,
            median_trip_duration_mins=excluded.median_trip_duration_mins,
            duration_samples=excluded.duration_samples,
            confidence_days=excluded.confidence_days,
            cycle_samples=excluded.cycle_samples,
            last_updated=excluded.last_updated
    """, (route_code, slot_count, cycle_mins, round(headway,2),
          median_duration, json.dumps(dur_samples),
          confidence, json.dumps(samples), computed_at))

    # Also store per-day pattern for reference
    conn.execute("""
        INSERT INTO rotation_patterns
            (route_code, service_date, slot_count, headway_mins, cycle_mins, computed_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(route_code, service_date) DO UPDATE SET
            slot_count=excluded.slot_count, headway_mins=excluded.headway_mins,
            cycle_mins=excluded.cycle_mins, computed_at=excluded.computed_at
    """, (route_code, service_date, slot_count, round(headway,2), cycle_mins, computed_at))

    return {"slot_count": slot_count, "headway_mins": round(headway,2),
            "cycle_mins": cycle_mins, "confidence_days": confidence}


# ── Phase 2: stable ordinal slot grid ────────────────────────────────────────

def build_slot_grid(conn, route_code: str, service_date: str,
                    slot_count: int) -> list[dict]:
    """
    Assign each scheduled departure (sorted by time) to a slot, ordinally.
    Returns [{departure_time, slot_number}, ...] — the stable καρτελάκι pattern.
    """
    sched_rows = conn.execute("""
        SELECT departure_time FROM scheduled_trips
        WHERE route_code=? AND schedule_date=? AND departure_time IS NOT NULL
        GROUP BY departure_time
        ORDER BY departure_time
    """, (route_code, service_date)).fetchall()

    grid = []
    for i, r in enumerate(sched_rows):
        grid.append({
            "departure_time": r["departure_time"],
            "slot_number":    (i % slot_count) + 1,
        })
    return grid


# ── Phase 3: order-preserving DP alignment ───────────────────────────────────

def align_trips_to_slots(actual_deps: list[float],
                         scheduled: list[float]) -> list[int | None]:
    """
    Order-preserving alignment of actual departures to scheduled departures.

    actual_deps: minutes-since-midnight of each actual trip (sorted)
    scheduled:   minutes-since-midnight of each scheduled departure (sorted)

    Returns, for each actual departure, the INDEX into `scheduled` it was
    assigned to (or None if it matched nothing within tolerance).

    Uses DP minimising total |actual - scheduled| while preserving order and
    allowing scheduled slots to be skipped (a missing bus). This is robust to
    uniform lateness: a whole batch of late buses still maps to their own
    slots rather than each slipping to the next.
    """
    n, m = len(actual_deps), len(scheduled)
    if n == 0 or m == 0:
        return [None]*n

    INF = float("inf")
    SKIP_ACTUAL_COST = MAX_LATE_MINS + 1   # cost of leaving an actual unmatched
    # dp[i][j] = min cost aligning first i actuals using first j scheduled
    dp = [[INF]*(m+1) for _ in range(n+1)]
    back = [[None]*(m+1) for _ in range(n+1)]
    dp[0][0] = 0.0
    for j in range(m+1):
        dp[0][j] = 0.0   # leftover scheduled slots are free (unfilled)

    for i in range(1, n+1):
        for j in range(1, m+1):
            # Option A: skip scheduled slot j-1 (no bus served it)
            if dp[i][j-1] < dp[i][j]:
                dp[i][j] = dp[i][j-1]
                back[i][j] = ("skip_sched", i, j-1)
            # Option B: match actual i-1 to scheduled j-1 (asymmetric cost)
            cost = match_cost(actual_deps[i-1], scheduled[j-1])
            if cost != INF:
                c = dp[i-1][j-1] + cost
                if c < dp[i][j]:
                    dp[i][j] = c
                    back[i][j] = ("match", i-1, j-1)
            # Option C: actual i-1 matches nothing (penalty)
            c = dp[i-1][j] + SKIP_ACTUAL_COST
            if c < dp[i][j]:
                dp[i][j] = c
                back[i][j] = ("skip_actual", i-1, j)

    # Backtrack
    assign = [None]*n
    i, j = n, m
    # find best j at row n
    best_j, best_val = m, dp[n][m]
    for jj in range(m+1):
        if dp[n][jj] < best_val:
            best_val, best_j = dp[n][jj], jj
    j = best_j
    while i > 0 and j > 0:
        step = back[i][j]
        if step is None:
            i -= 1
            continue
        kind = step[0]
        if kind == "match":
            assign[step[1]] = step[2]
            i, j = step[1], step[2]
        elif kind == "skip_sched":
            j = step[2]
        else:  # skip_actual
            i = step[1]
    return assign


def assign_slots(conn, route_code: str, service_date: str,
                 slot_count: int, computed_at: str) -> dict:
    grid = build_slot_grid(conn, route_code, service_date, slot_count)
    if not grid:
        return {"assigned": 0, "handoffs": 0}

    sched_mins = [_time_to_mins(g["departure_time"]) for g in grid]

    trip_rows = conn.execute("""
        SELECT id, vehicle_no, started_at, ended_at FROM trips
        WHERE route_code=? AND service_date=? ORDER BY started_at
    """, (route_code, service_date)).fetchall()

    if not trip_rows:
        return {"assigned": 0, "handoffs": 0}

    actual_mins = [_iso_to_mins_since_midnight(t["started_at"]) for t in trip_rows]
    assignment  = align_trips_to_slots(actual_mins, sched_mins)

    n_assigned = n_handoffs = 0
    last_vehicle_per_slot: dict[int, tuple[str, str]] = {}

    for trip, sched_idx, amins in zip(trip_rows, assignment, actual_mins):
        if sched_idx is None:
            continue
        g = grid[sched_idx]
        slot_num   = g["slot_number"]
        sched_time = g["departure_time"]
        deviation  = round(amins - sched_mins[sched_idx], 1)

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
              sched_time, deviation, computed_at))
        n_assigned += 1

        # Handoff detection
        if slot_num in last_vehicle_per_slot:
            prev_veh, prev_ended = last_vehicle_per_slot[slot_num]
            if prev_veh != trip["vehicle_no"] and prev_ended:
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
                          prev_veh, trip["vehicle_no"], trip["started_at"],
                          round(gap,1) if gap else None, computed_at))
                    n_handoffs += 1

        last_vehicle_per_slot[slot_num] = (trip["vehicle_no"],
                                           trip["ended_at"] or trip["started_at"])

    return {"assigned": n_assigned, "handoffs": n_handoffs}


# ── vehicle activity ─────────────────────────────────────────────────────────

def build_vehicle_activity(conn, service_date: str, computed_at: str):
    conn.execute("DELETE FROM vehicle_activity WHERE service_date=?", (service_date,))
    rows = conn.execute("""
        SELECT t.vehicle_no, t.route_code, sa.slot_number,
               COUNT(*) AS trip_count,
               MIN(t.started_at) AS first_dep, MAX(t.started_at) AS last_dep,
               SUM((strftime('%s',COALESCE(t.ended_at,t.started_at))
                    -strftime('%s',t.started_at))/60.0) AS total_mins
        FROM trips t
        LEFT JOIN slot_assignments sa ON sa.trip_id=t.id
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


# ── persistent slot definitions (typical times per slot) ─────────────────────

def update_slot_definitions(conn, route_code: str, service_date: str,
                            slot_count: int, computed_at: str):
    grid = build_slot_grid(conn, route_code, service_date, slot_count)
    by_slot: dict[int, list[str]] = {}
    for g in grid:
        by_slot.setdefault(g["slot_number"], []).append(g["departure_time"])

    for slot_num, times in by_slot.items():
        times.sort()
        first_dep = times[0][:5]
        intervals = []
        for i in range(1, len(times)):
            t1, t2 = _time_to_mins(times[i-1]), _time_to_mins(times[i])
            if t2 > t1:
                intervals.append(t2-t1)
        avg_interval = round(statistics.mean(intervals),1) if intervals else None
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


# ── orchestration ────────────────────────────────────────────────────────────

def compute_all_slots(conn, service_date: str, computed_at: str) -> dict:
    routes = conn.execute("SELECT route_code FROM routes").fetchall()
    n_patterns = n_assigned = n_handoffs = 0

    for r in routes:
        rc = r["route_code"]
        try:
            rot = update_route_rotation(conn, rc, service_date, computed_at)
            if not rot:
                continue
            n_patterns += 1
            slot_count = rot["slot_count"]
            result = assign_slots(conn, rc, service_date, slot_count, computed_at)
            n_assigned += result["assigned"]
            n_handoffs += result["handoffs"]
            update_slot_definitions(conn, rc, service_date, slot_count, computed_at)
        except Exception as e:
            log.warning("Slot computation failed for route %s: %s", rc, e)

    build_vehicle_activity(conn, service_date, computed_at)
    return {"patterns": n_patterns, "assigned": n_assigned, "handoffs": n_handoffs}
