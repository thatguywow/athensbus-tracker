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
    if _ATHENS is not None:
        d = date.fromisoformat(service_date)
        start_local = datetime(d.year, d.month, d.day, tzinfo=_ATHENS)
        end_local = start_local + timedelta(days=1)
        return (start_local.astimezone(timezone.utc).isoformat(),
                end_local.astimezone(timezone.utc).isoformat())
    return (f"{service_date}T00:00:00", f"{service_date}T23:59:59.999999")


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

    start_bound, end_bound = _athens_window(service_date)
    rows = conn.execute("""
        SELECT vehicle_no, stop_code, stop_order, passed_at
        FROM stop_passages
        WHERE route_code=? AND passed_at>=? AND passed_at<?
        ORDER BY vehicle_no, passed_at
    """, (route_code, start_bound, end_bound)).fetchall()

    by_vehicle: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["stop_order"] is None:
            continue
        by_vehicle[r["vehicle_no"]].append(dict(r))

    n_trips = n_departures = 0
    distinct_vehicles = set()

    for vehicle_no, plist in by_vehicle.items():
        distinct_vehicles.add(vehicle_no)
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
