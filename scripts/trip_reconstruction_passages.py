"""
trip_reconstruction_passages.py — reconstruct trips purely from stop_passages.

No GPS. Mirrors fragkakis' TripExtractor: chain a vehicle's stop passages on
one route by increasing stop_order into trips, then derive precise
departure/arrival from the edge passages.

DEPARTURE (started_at, NOT NULL):
  1. passage AT the origin stop (min stop_order)              → its time
  2. linear regression on >=2 origin-side passages            → extrapolate to origin
  3. one origin-side passage                                  → its time
  4. only terminus-side seen + known route duration           → terminus - duration
  5. fallback                                                 → first passage time

ARRIVAL (terminus_arrived_at, nullable; NULL = trip never completed):
  1. passage AT the terminus stop (max stop_order)            → its time
  2. linear regression on >=2 terminus-side passages          → extrapolate to terminus
  3. otherwise                                                → NULL (incomplete)

A trip is "complete" iff we have terminus-side evidence (arrival not NULL).

Produces the SAME rows as the GPS reconstruct_route_day:
  trips, trip_stop_times, vehicle_departures — same columns, same return shape.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date
try:
    from zoneinfo import ZoneInfo
    _ATHENS = ZoneInfo("Europe/Athens")
except Exception:
    _ATHENS = None

log = logging.getLogger("trip_reconstruction_passages")

TRIP_GAP_MINUTES = 25   # gap between consecutive passages that splits trips
OVERLAP_HOURS    = 3    # read past the 04:00 day end so 04:00-crossing trips stay whole
MIN_DURATION_FRACTION = 0.3   # arrival implying < 30% of typical duration → incomplete
BOUNDARY_ZONE_MINS = 20  # departures within ±20' of 04:00: owner day decided by
                         # whichever day's SCHEDULE has the nearest slot (a 03:55
                         # slot leaving late at 04:05 stays on yesterday)


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _linfit_predict(points: list[tuple[int, datetime]], x_target: int):
    """
    Least-squares fit of time (seconds) vs stop_order, predict time at x_target.
    points: list of (stop_order, datetime). Needs >=2 distinct stop_orders.
    Returns a datetime or None.
    """
    xs = [p[0] for p in points]
    if len(set(xs)) < 2:
        return None
    t0 = min(p[1] for p in points)
    xy = [(x, (t - t0).total_seconds()) for x, t in points]
    n = len(xy)
    sx = sum(x for x, _ in xy)
    sy = sum(y for _, y in xy)
    sxy = sum(x*y for x, y in xy)
    sxx = sum(x*x for x, _ in xy)
    denom = n*sxx - sx*sx
    if abs(denom) < 1e-9:
        return None
    b = (n*sxy - sx*sy) / denom      # seconds per stop_order
    a = (sy - b*sx) / n
    return t0 + timedelta(seconds=a + b*x_target)


def _athens_window(service_date: str) -> tuple[str, str]:
    """UTC window of one service day: D 04:00 Athens → D+1 04:00 Athens."""
    start_h = 4
    try:
        from db import SERVICE_DAY_START_HOUR as start_h  # single source of truth
    except Exception:
        pass
    if _ATHENS is not None:
        d = date.fromisoformat(service_date)
        start_local = datetime(d.year, d.month, d.day, start_h, 0, tzinfo=_ATHENS)
        end_local = start_local + timedelta(days=1)
        return (start_local.astimezone(timezone.utc).isoformat(),
                end_local.astimezone(timezone.utc).isoformat())
    d = date.fromisoformat(service_date)
    d2 = d + timedelta(days=1)
    return (f"{d.isoformat()}T{start_h:02d}:00:00",
            f"{d2.isoformat()}T{start_h:02d}:00:00")


def _sched_datetimes(conn, route_code: str, sched_date: str) -> list[datetime]:
    """Scheduled departures of one service day as aware datetimes (Athens).
    Times before 04:00 belong to the END of that service day (calendar +1)."""
    out = []
    try:
        rows = conn.execute(
            "SELECT departure_time FROM scheduled_trips "
            "WHERE route_code=? AND schedule_date=?",
            (route_code, sched_date)).fetchall()
    except Exception:
        return out
    d0 = date.fromisoformat(sched_date)
    for r in rows:
        t = r["departure_time"]
        try:
            h, m = int(t[:2]), int(t[3:5])
        except (ValueError, TypeError):
            continue
        d = d0 + timedelta(days=1) if h < 4 else d0
        if _ATHENS is not None:
            out.append(datetime(d.year, d.month, d.day, h, m, tzinfo=_ATHENS))
        else:
            out.append(datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc))
    return out


def _nearest_secs(dts: list[datetime], t: datetime):
    if not dts:
        return None
    return min(abs((d - t).total_seconds()) for d in dts)


def _boundary_owner_is_this_day(conn, route_code: str, started_dt: datetime,
                                this_date: str, other_date: str,
                                default_this: bool) -> bool:
    """
    For a departure near the 04:00 boundary, the trip belongs to the day whose
    SCHEDULE has the nearest slot: a 03:55 slot departing late at 04:05 stays on
    yesterday; a 04:05 slot departing at 04:08 stays on today. Deterministic, so
    both days' reconstructions reach the same verdict (no duplicates, no gaps).
    Falls back to the actual-departure rule when either schedule is missing.
    """
    mine = _nearest_secs(_sched_datetimes(conn, route_code, this_date), started_dt)
    theirs = _nearest_secs(_sched_datetimes(conn, route_code, other_date), started_dt)
    if mine is None or theirs is None:
        return default_this
    if mine == theirs:
        return default_this
    return mine < theirs


def _split_trips(passages: list[dict], route_duration: float | None) -> list[list[dict]]:
    """
    Chain one vehicle's passages (already sorted by passed_at) into trips.

    Edge-only tracking means the middle of the route is unobserved, so the time
    gap between the origin-side cluster and the terminus-side cluster is normally
    large (the whole traversal). Therefore we split a new trip when stop_order
    does NOT advance (a reset back toward the origin), and only use a time gap as
    a secondary guard when it exceeds a generous span (≈1.5× the route duration),
    which catches "advancing but clearly a different trip hours later" cases.
    """
    gap_limit = (route_duration * 1.5) if route_duration else 90.0
    gap_limit = max(gap_limit, 60.0)

    trips: list[list[dict]] = []
    cur: list[dict] = []
    for p in passages:
        if not cur:
            cur = [p]
            continue
        prev = cur[-1]
        gap = (_parse(p["passed_at"]) - _parse(prev["passed_at"])).total_seconds() / 60
        if p["stop_order"] <= prev["stop_order"] or gap > gap_limit:
            trips.append(cur)
            cur = [p]
        else:
            cur.append(p)
    if cur:
        trips.append(cur)
    return trips


def reconstruct_route_day_from_passages(conn, route_code: str, service_date: str,
                                        computed_at: str) -> dict:
    """Reconstruct trips for one route/day from stop_passages. Idempotent."""
    # ── cleanup (same contract as GPS version) ──
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM trips WHERE route_code=? AND service_date=?",
        (route_code, service_date)
    ).fetchall()]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        conn.execute(f"DELETE FROM trip_stop_times WHERE trip_id IN ({ph})", old_ids)
        conn.execute("DELETE FROM slot_assignments WHERE route_code=? AND service_date=?",
                     (route_code, service_date))
        conn.execute("DELETE FROM vehicle_departures WHERE route_code=? AND service_date=?",
                     (route_code, service_date))
        conn.execute("DELETE FROM trips WHERE route_code=? AND service_date=?",
                     (route_code, service_date))

    # route stop_order bounds
    bounds = conn.execute(
        "SELECT MIN(stop_order) lo, MAX(stop_order) hi FROM stops WHERE route_code=?",
        (route_code,)
    ).fetchone()
    if not bounds or bounds["lo"] is None:
        return {"route_code": route_code, "trips": 0, "departures": 0, "distinct_vehicles": 0}
    lo, hi = bounds["lo"], bounds["hi"]
    mid = (lo + hi) / 2.0

    # persistent route duration (for backward extrapolation when origin unseen)
    route_duration = None
    try:
        row = conn.execute(
            "SELECT median_trip_duration_mins FROM route_rotation WHERE route_code=?",
            (route_code,)
        ).fetchone()
        if row and row["median_trip_duration_mins"]:
            route_duration = float(row["median_trip_duration_mins"])
    except Exception:
        pass

    # Read passages in an EXTENDED window: the service day plus a few hours past
    # its 04:00 end, so a trip that departs before 04:00 but finishes after it
    # (e.g. dep 03:40 → arr 04:30) is assembled WHOLE. After building trips we
    # keep only those whose DEPARTURE falls inside the service day — a trip
    # belongs to the day its shift departed. The next day's reconstruction sees
    # the same tail passages but drops the trip (departure before its window),
    # so each trip lands in exactly one day.
    start_bound, end_bound = _athens_window(service_date)
    day_start, day_end = _parse(start_bound), _parse(end_bound)
    # Query extends past BOTH edges: after the end (so 04:00-crossing trips stay
    # whole) and slightly before the start (so a boundary-zone trip departing
    # e.g. 03:55 can be assembled whole here too, in case the schedule assigns
    # it to this day).
    query_start = (day_start - timedelta(minutes=BOUNDARY_ZONE_MINS + 10)).isoformat()
    query_end = (day_end + timedelta(hours=OVERLAP_HOURS)).isoformat()
    rows = conn.execute("""
        SELECT vehicle_no, stop_code, stop_order, passed_at
        FROM stop_passages
        WHERE route_code=? AND passed_at>=? AND passed_at<?
        ORDER BY vehicle_no, passed_at
    """, (route_code, query_start, query_end)).fetchall()

    by_vehicle: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["stop_order"] is None:
            continue
        by_vehicle[r["vehicle_no"]].append(dict(r))

    n_trips = n_departures = 0
    distinct_vehicles = set()

    for vehicle_no, plist in by_vehicle.items():
        for trip in _split_trips(plist, route_duration):
            if not trip:
                continue

            origin_side = [(p["stop_order"], _parse(p["passed_at"]))
                           for p in trip if p["stop_order"] <= mid]
            term_side = [(p["stop_order"], _parse(p["passed_at"]))
                         for p in trip if p["stop_order"] > mid]
            first_dt = _parse(trip[0]["passed_at"])
            last_dt = _parse(trip[-1]["passed_at"])

            # ── DEPARTURE ──
            origin_hit = next((p for p in trip if p["stop_order"] == lo), None)
            if origin_hit:
                started_dt = _parse(origin_hit["passed_at"])
            elif len(origin_side) >= 2:
                started_dt = _linfit_predict(origin_side, lo) or origin_side[0][1]
            elif origin_side:
                started_dt = origin_side[0][1]
            elif term_side and route_duration:
                started_dt = term_side[-1][1] - timedelta(minutes=route_duration)
            else:
                started_dt = first_dt

            # ── ARRIVAL ──
            term_hit = next((p for p in trip if p["stop_order"] == hi), None)
            if term_hit:
                terminus_dt = _parse(term_hit["passed_at"])
            elif len(term_side) >= 2:
                terminus_dt = _linfit_predict(term_side, hi)
            else:
                terminus_dt = None   # incomplete — never observed finishing

            # guard: arrival must be after departure
            if terminus_dt and terminus_dt <= started_dt:
                terminus_dt = None

            # sanity net (subordinate to real data): an arrival implying a trip
            # far shorter than physically possible (< MIN_DURATION_FRACTION of
            # the route's typical duration) is a withdrawal artifact — the
            # vehicle vanished and its terminus prediction was misread as a
            # passage. Mark incomplete ("—") instead of showing a fake Λήξη.
            if terminus_dt and route_duration:
                dur_mins = (terminus_dt - started_dt).total_seconds() / 60
                if dur_mins < MIN_DURATION_FRACTION * route_duration:
                    terminus_dt = None

            # ── DAY OWNERSHIP ──
            # Normally: a trip belongs to the day it DEPARTED. Near the 04:00
            # boundary (±BOUNDARY_ZONE_MINS), the day whose schedule has the
            # nearest slot wins — so a delayed 03:55 slot leaving 04:05 stays on
            # yesterday, and an early 04:00 slot leaving 03:57 moves to today.
            zone = timedelta(minutes=BOUNDARY_ZONE_MINS)
            in_day = day_start <= started_dt < day_end
            if abs(started_dt - day_end) <= zone:
                nxt = (date.fromisoformat(service_date) + timedelta(days=1)).isoformat()
                keep = _boundary_owner_is_this_day(
                    conn, route_code, started_dt, service_date, nxt,
                    default_this=in_day)
            elif abs(started_dt - day_start) <= zone:
                prv = (date.fromisoformat(service_date) - timedelta(days=1)).isoformat()
                keep = _boundary_owner_is_this_day(
                    conn, route_code, started_dt, service_date, prv,
                    default_this=in_day)
            else:
                keep = in_day
            if not keep:
                continue

            started_at = started_dt.isoformat()
            ended_at = last_dt.isoformat()          # last observed passage (NOT NULL)
            terminus_val = terminus_dt.isoformat() if terminus_dt else None
            stop_count = len(trip)

            cur = conn.execute("""
                INSERT INTO trips
                    (route_code, vehicle_no, service_date, started_at, ended_at,
                     terminus_arrived_at, stop_count, computed_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (route_code, vehicle_no, service_date,
                  started_at, ended_at, terminus_val, stop_count, computed_at))
            trip_id = cur.lastrowid
            n_trips += 1
            distinct_vehicles.add(vehicle_no)

            for p in trip:
                conn.execute("""
                    INSERT INTO trip_stop_times
                        (trip_id, stop_code, stop_order, passed_at, distance_m, method)
                    VALUES (?,?,?,?,?,?)
                """, (trip_id, p["stop_code"], p["stop_order"], p["passed_at"],
                      0.0, "passage"))

            conn.execute("""
                INSERT INTO vehicle_departures
                    (vehicle_no, route_code, service_date, departed_at, trip_id, computed_at)
                VALUES (?,?,?,?,?,?)
            """, (vehicle_no, route_code, service_date, started_at, trip_id, computed_at))
            n_departures += 1

    return {
        "route_code":        route_code,
        "trips":             n_trips,
        "departures":        n_departures,
        "distinct_vehicles": len(distinct_vehicles),
    }
