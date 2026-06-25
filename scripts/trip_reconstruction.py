"""
trip_reconstruction.py — GPS pings → trips with interpolated stop times.

STOP TIME METHOD (two passes):
  Pass 1: Linear interpolation between consecutive ping pairs.
          Any stop projecting onto the A→B segment gets a proportional timestamp.
          Handles multiple stops between two polls naturally.
  Pass 2: Snap-to-nearest-ping fallback for terminus/stationary stops.

TERMINUS PRECISION:
  If terminus_observations are available for a route (from getStopArrivals),
  the first and last stop times are replaced with OASA's own predicted times,
  converted to actuals when the vehicle is observed arriving. This gives
  precise departure and arrival times independent of polling resolution.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("trip_reconstruction")

TRIP_GAP_MINUTES     = 25
MAX_MATCH_DISTANCE_M = 400
MAX_LATERAL_DISTANCE_M = 250


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi   = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (math.sin(dphi/2)**2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2)
    return 2 * R * math.asin(min(1, math.sqrt(a)))


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def split_into_trips(pings: list[dict]) -> list[list[dict]]:
    if not pings:
        return []
    trips = [[pings[0]]]
    for prev, cur in zip(pings, pings[1:]):
        gap = (parse_iso(cur["ts_utc"]) - parse_iso(prev["ts_utc"])).total_seconds() / 60
        if gap > TRIP_GAP_MINUTES:
            trips.append([cur])
        else:
            trips[-1].append(cur)
    return trips


def _interpolate_pass_time(p_a, p_b, stop_lat, stop_lng):
    lat_ref = math.radians(p_a["lat"])
    mpd_lat = 111320.0
    mpd_lng = 111320.0 * math.cos(lat_ref)

    bx = (p_b["lng"] - p_a["lng"]) * mpd_lng
    by = (p_b["lat"] - p_a["lat"]) * mpd_lat
    sx = (stop_lng  - p_a["lng"]) * mpd_lng
    sy = (stop_lat  - p_a["lat"]) * mpd_lat

    seg_len_sq = bx*bx + by*by
    if seg_len_sq < 1.0:
        return None

    t = (sx*bx + sy*by) / seg_len_sq
    if not (0.0 <= t <= 1.0):
        return None

    lateral_m = math.sqrt((sx - t*bx)**2 + (sy - t*by)**2)
    if lateral_m > MAX_LATERAL_DISTANCE_M:
        return None

    t_a = parse_iso(p_a["ts_utc"])
    t_b = parse_iso(p_b["ts_utc"])
    interp = t_a + timedelta(seconds=(t_b - t_a).total_seconds() * t)
    return interp.isoformat(), round(lateral_m, 1)


def match_stops_to_trip(trip_pings: list[dict], stops: list[dict],
                        origin_observed_time: str | None = None,
                        terminus_observed_time: str | None = None) -> list[dict]:
    """
    Match stops to trip pings using interpolation + snap fallback.
    If origin/terminus observed times are provided (from getStopArrivals),
    they override the computed time for stop_order 1 and the last stop.
    """
    if not trip_pings or not stops:
        return []

    results: dict[int, dict] = {}
    matched_orders = set()
    stop_cursor = 0

    # Pass 1: interpolation
    for i in range(len(trip_pings) - 1):
        p_a, p_b = trip_pings[i], trip_pings[i+1]
        for j in range(stop_cursor, len(stops)):
            stop = stops[j]
            if stop["lat"] is None or stop["lng"] is None:
                continue
            result = _interpolate_pass_time(p_a, p_b, stop["lat"], stop["lng"])
            if result is not None:
                passed_at, lateral_m = result
                results[stop["stop_order"]] = {
                    "stop_order": stop["stop_order"],
                    "stop_code":  stop["stop_code"],
                    "passed_at":  passed_at,
                    "distance_m": lateral_m,
                    "method":     "interpolated",
                }
                matched_orders.add(stop["stop_order"])
        while stop_cursor < len(stops) and stops[stop_cursor]["stop_order"] in matched_orders:
            stop_cursor += 1

    # Pass 2: snap fallback
    unmatched = [s for s in stops
                 if s["stop_order"] not in matched_orders
                 and s["lat"] is not None and s["lng"] is not None]
    if unmatched:
        snap_start = 0
        for stop in unmatched:
            best_idx, best_dist = None, None
            for idx in range(snap_start, len(trip_pings)):
                d = haversine_m(trip_pings[idx]["lat"], trip_pings[idx]["lng"],
                                stop["lat"], stop["lng"])
                if best_dist is None or d < best_dist:
                    best_dist, best_idx = d, idx
            if best_dist is not None and best_dist <= MAX_MATCH_DISTANCE_M:
                results[stop["stop_order"]] = {
                    "stop_order": stop["stop_order"],
                    "stop_code":  stop["stop_code"],
                    "passed_at":  trip_pings[best_idx]["ts_utc"],
                    "distance_m": round(best_dist, 1),
                    "method":     "snapped",
                }
                snap_start = best_idx

    # Pass 3: override with precise terminus observations if available
    if results:
        first_order = min(results.keys())
        last_order  = max(results.keys())

        if origin_observed_time and first_order in results:
            results[first_order]["passed_at"] = origin_observed_time
            results[first_order]["method"]    = "terminus_observed"
            results[first_order]["distance_m"] = 0.0

        if terminus_observed_time and last_order in results:
            results[last_order]["passed_at"] = terminus_observed_time
            results[last_order]["method"]    = "terminus_observed"
            results[last_order]["distance_m"] = 0.0

    return sorted(results.values(), key=lambda x: x["stop_order"])


def get_terminus_observed_times(conn, route_code: str, vehicle_no: str,
                                trip_start: str, trip_end: str) -> tuple[str | None, str | None]:
    """
    Look up terminus_observations for this vehicle on this route around this trip's window.
    Returns (origin_time_iso, terminus_time_iso) or (None, None) if not available.
    """
    # Origin: find observation for the origin stop within 10 min before trip start
    window_start = (parse_iso(trip_start) - timedelta(minutes=10)).isoformat()

    origin_stop = conn.execute("""
        SELECT stop_code FROM stops
        WHERE route_code = ? AND stop_order = (
            SELECT MIN(stop_order) FROM stops WHERE route_code = ?
        )
    """, (route_code, route_code)).fetchone()

    terminus_stop = conn.execute("""
        SELECT stop_code FROM stops
        WHERE route_code = ? AND stop_order = (
            SELECT MAX(stop_order) FROM stops WHERE route_code = ?
        )
    """, (route_code, route_code)).fetchone()

    origin_time = None
    if origin_stop:
        row = conn.execute("""
            SELECT observed_at, predicted_mins FROM terminus_observations
            WHERE route_code = ? AND stop_code = ? AND stop_type = 'origin'
              AND (vehicle_no = ? OR vehicle_no = '')
              AND observed_at BETWEEN ? AND ?
            ORDER BY ABS(predicted_mins) ASC
            LIMIT 1
        """, (route_code, origin_stop["stop_code"], vehicle_no,
              window_start, trip_start)).fetchone()
        if row:
            # actual departure ≈ observed_at + predicted_mins
            try:
                actual = (parse_iso(row["observed_at"]) +
                          timedelta(minutes=row["predicted_mins"]))
                origin_time = actual.isoformat()
            except Exception:
                pass

    terminus_time = None
    if terminus_stop:
        window_end = (parse_iso(trip_end) + timedelta(minutes=10)).isoformat()
        row = conn.execute("""
            SELECT observed_at, predicted_mins FROM terminus_observations
            WHERE route_code = ? AND stop_code = ? AND stop_type = 'terminus'
              AND (vehicle_no = ? OR vehicle_no = '')
              AND observed_at BETWEEN ? AND ?
            ORDER BY ABS(predicted_mins) ASC
            LIMIT 1
        """, (route_code, terminus_stop["stop_code"], vehicle_no,
              trip_end, window_end)).fetchone()
        if row:
            try:
                actual = (parse_iso(row["observed_at"]) +
                          timedelta(minutes=row["predicted_mins"]))
                terminus_time = actual.isoformat()
            except Exception:
                pass

    return origin_time, terminus_time


def reconstruct_route_day(conn, route_code: str, service_date: str,
                          computed_at: str) -> dict:
    """
    Reconstruct trips for one route on one service_date. Idempotent.
    """
    # Delete prior computed rows for this route/date
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

    start_bound = f"{service_date}T00:00:00"
    end_bound   = f"{service_date}T23:59:59.999999"

    ping_rows = conn.execute("""
        SELECT vehicle_no, lat, lng, ts_utc FROM vehicle_pings
        WHERE route_code=? AND ts_utc>=? AND ts_utc<=?
        ORDER BY vehicle_no, ts_utc
    """, (route_code, start_bound, end_bound)).fetchall()

    stop_rows = conn.execute("""
        SELECT stop_order, stop_code, lat, lng FROM stops
        WHERE route_code=? ORDER BY stop_order
    """, (route_code,)).fetchall()
    stops = [dict(r) for r in stop_rows]

    by_vehicle: dict[str, list[dict]] = defaultdict(list)
    for r in ping_rows:
        by_vehicle[r["vehicle_no"]].append(dict(r))

    n_trips = n_departures = 0
    distinct_vehicles = set()

    for vehicle_no, pings in by_vehicle.items():
        distinct_vehicles.add(vehicle_no)
        for trip_pings in split_into_trips(pings):
            if len(trip_pings) < 2:
                continue

            started_at = trip_pings[0]["ts_utc"]
            ended_at   = trip_pings[-1]["ts_utc"]

            # Get precise terminus times if available
            origin_time, terminus_time = get_terminus_observed_times(
                conn, route_code, vehicle_no, started_at, ended_at
            )

            matches = match_stops_to_trip(
                trip_pings, stops, origin_time, terminus_time
            ) if stops else []

            cur = conn.execute("""
                INSERT INTO trips
                    (route_code, vehicle_no, service_date, started_at, ended_at,
                     terminus_arrived_at, stop_count, computed_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (route_code, vehicle_no, service_date, started_at, ended_at,
                  terminus_time, len(matches), computed_at))
            trip_id = cur.lastrowid
            n_trips += 1

            for m in matches:
                conn.execute("""
                    INSERT INTO trip_stop_times
                        (trip_id, stop_code, stop_order, passed_at, distance_m, method)
                    VALUES (?,?,?,?,?,?)
                """, (trip_id, m["stop_code"], m["stop_order"],
                      m["passed_at"], m["distance_m"], m["method"]))

            if matches and matches[0]["stop_order"] == 1:
                conn.execute("""
                    INSERT INTO vehicle_departures
                        (vehicle_no, route_code, service_date, departed_at, trip_id, computed_at)
                    VALUES (?,?,?,?,?,?)
                """, (vehicle_no, route_code, service_date, started_at, trip_id, computed_at))
                n_departures += 1

    return {
        "route_code":       route_code,
        "trips":            n_trips,
        "departures":       n_departures,
        "distinct_vehicles": len(distinct_vehicles),
    }
