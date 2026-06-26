"""
trip_reconstruction.py — GPS pings → trips with precise departure/arrival times.

DEPARTURE TIME:
  The first ping near the origin stop is NOT necessarily the departure time —
  the bus may have been sitting there before moving. We detect departure by
  finding the moment the bus starts moving away from the origin (distance
  from origin increases consistently). We interpolate between the last
  stationary ping and the first moving ping to estimate the actual departure.
  If getStopArrivals data is available for the origin stop, it takes priority.

ARRIVAL TIME:
  Similarly, we detect arrival at the terminus by finding when the bus
  first comes within MAX_MATCH_DISTANCE_M of the last stop and stays there.
  We interpolate between the ping before arrival and the arrival ping.
  If getStopArrivals data is available, it takes priority.

MINIMUM COMPLETION THRESHOLD:
  A trip must cover at least MIN_ROUTE_COVERAGE_PCT of the route distance
  AND last at least MIN_TRIP_DURATION_MINS to be counted as a real trip.
  This filters out GPS fragments, stationary buses, and the X93-style
  "5 minute trip" artifact from sparse polling.

STOP TIMES:
  Two-pass: interpolation between ping pairs (handles multiple stops
  between polls), then snap-to-nearest fallback for terminus stops.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("trip_reconstruction")

TRIP_GAP_MINUTES        = 25
MAX_MATCH_DISTANCE_M    = 400
MAX_LATERAL_DISTANCE_M  = 250
MIN_TRIP_DURATION_MINS  = 10    # absolute minimum realistic trip length
MIN_PROGRESS_SPAN       = 0.50  # must traverse at least 50% of the route start→end
MIN_MOVEMENT_M          = 200   # must move at least 200m to count as departed
PROGRESS_RESET_PEAK     = 0.60  # progress must reach this before a reset counts
PROGRESS_RESET_LOW      = 0.30  # ...then drop below this = new trip starting


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(min(1, math.sqrt(a)))


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def split_into_trips(pings: list[dict]) -> list[list[dict]]:
    """Split a vehicle's pings into segments on large time gaps only."""
    if not pings:
        return []
    trips = [[pings[0]]]
    for prev, cur in zip(pings, pings[1:]):
        gap = (parse_iso(cur["ts_utc"]) - parse_iso(prev["ts_utc"])).total_seconds()/60
        if gap > TRIP_GAP_MINUTES:
            trips.append([cur])
        else:
            trips[-1].append(cur)
    return trips


def compute_progress(ping: dict, stops: list[dict]) -> tuple[float | None, float | None]:
    """
    Returns (progress_fraction, distance_to_nearest_stop_m).
    progress_fraction: 0.0 at origin, 1.0 at terminus, based on which stop
    (in route order) the ping is physically nearest to.
    """
    best_order = best_dist = None
    for s in stops:
        if s["lat"] is None or s["lng"] is None:
            continue
        d = haversine_m(ping["lat"], ping["lng"], s["lat"], s["lng"])
        if best_dist is None or d < best_dist:
            best_dist = d
            best_order = s["stop_order"]
    if best_order is None:
        return None, None
    orders = [s["stop_order"] for s in stops]
    lo, hi = min(orders), max(orders)
    span = hi - lo
    progress = (best_order - lo) / span if span > 0 else 0.0
    return progress, best_dist


def segment_into_trips(pings: list[dict], stops: list[dict]) -> list[list[dict]]:
    """
    Split a vehicle's pings into individual trips using BOTH:
      1. Large time gaps (>TRIP_GAP_MINUTES)
      2. Route-progress resets (bus reaches near-terminus then jumps back to
         near-origin = it started a new trip)

    This correctly separates back-to-back trips (origin→terminus→origin→terminus)
    that have no time gap between them, which simple time-gap splitting misses.
    """
    time_segments = split_into_trips(pings)

    if not stops or len(stops) < 4:
        return time_segments

    result = []
    for seg in time_segments:
        current = []
        peak_progress = 0.0
        for p in seg:
            prog, _ = compute_progress(p, stops)
            if prog is None:
                current.append(p)
                continue
            # Detect a reset: we climbed near the terminus, now back near origin
            if peak_progress >= PROGRESS_RESET_PEAK and prog <= PROGRESS_RESET_LOW:
                if current:
                    result.append(current)
                current = [p]
                peak_progress = prog
            else:
                current.append(p)
                peak_progress = max(peak_progress, prog)
        if current:
            result.append(current)
    return result


def estimate_endpoints_by_progress(trip_pings: list[dict],
                                   stops: list[dict]) -> tuple[str | None, str | None]:
    """
    Estimate origin-departure and terminus-arrival times by extrapolating
    the bus's OWN observed speed (route-progress per minute) to the endpoints.

    This is the key fix for sparse data: if a bus was only caught mid-route
    (e.g. we first saw it at 50% progress at 20:45), the actual origin
    departure was earlier. Using the rate of progress between pings, we
    extrapolate backward to progress=0 (departure) and forward to
    progress=1 (arrival).

    Assumes roughly constant speed — good enough for ±1-3 min on urban routes,
    far better than using the first/last ping time when endpoints weren't seen.

    Returns (departure_iso, arrival_iso) or (None, None) if it can't compute.
    """
    if not stops or len(stops) < 4 or len(trip_pings) < 2:
        return None, None

    pts = []  # (minutes_since_first_ping, progress)
    t0 = parse_iso(trip_pings[0]["ts_utc"])
    for p in trip_pings:
        prog, dist = compute_progress(p, stops)
        if prog is not None and dist is not None and dist <= MAX_MATCH_DISTANCE_M * 2:
            mins = (parse_iso(p["ts_utc"]) - t0).total_seconds() / 60
            pts.append((mins, prog))

    if len(pts) < 2:
        return None, None

    # Linear least-squares fit: progress = a + b * minutes
    n = len(pts)
    sum_x = sum(x for x, _ in pts)
    sum_y = sum(y for _, y in pts)
    sum_xy = sum(x*y for x, y in pts)
    sum_xx = sum(x*x for x, _ in pts)
    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-9:
        return None, None
    b = (n * sum_xy - sum_x * sum_y) / denom   # progress per minute
    a = (sum_y - b * sum_x) / n                 # progress at first ping

    if b <= 1e-6:
        return None, None  # not moving forward — can't extrapolate

    # Solve for minutes where progress = 0 (departure) and progress = 1 (arrival)
    mins_at_origin   = (0.0 - a) / b
    mins_at_terminus = (1.0 - a) / b

    departure = (t0 + timedelta(minutes=mins_at_origin)).isoformat()
    arrival   = (t0 + timedelta(minutes=mins_at_terminus)).isoformat()
    return departure, arrival


def estimate_departure_time(trip_pings: list[dict],
                             origin_lat: float | None,
                             origin_lng: float | None) -> str:
    """
    Estimate when the bus actually departed the origin stop.
    Finds the last ping where the bus was still near the origin,
    then interpolates to when it started moving.
    Falls back to first ping timestamp if origin coords unavailable.
    """
    if origin_lat is None or origin_lng is None or len(trip_pings) < 2:
        return trip_pings[0]["ts_utc"]

    # Find the last ping where bus was still within 2x match distance of origin
    last_stationary_idx = 0
    for i, p in enumerate(trip_pings):
        dist = haversine_m(p["lat"], p["lng"], origin_lat, origin_lng)
        if dist <= MAX_MATCH_DISTANCE_M * 2:
            last_stationary_idx = i
        else:
            break  # once it moves away, stop looking

    # If it never moved away from origin, use first ping
    if last_stationary_idx >= len(trip_pings) - 1:
        return trip_pings[0]["ts_utc"]

    # Interpolate departure between last_stationary and next ping
    p_a = trip_pings[last_stationary_idx]
    p_b = trip_pings[last_stationary_idx + 1]
    dist_a = haversine_m(p_a["lat"], p_a["lng"], origin_lat, origin_lng)
    dist_b = haversine_m(p_b["lat"], p_b["lng"], origin_lat, origin_lng)

    if dist_b <= dist_a or dist_b - dist_a < 50:
        return p_b["ts_utc"]

    # Linear interpolation: t = time when distance = MAX_MATCH_DISTANCE_M
    t = (MAX_MATCH_DISTANCE_M - dist_a) / (dist_b - dist_a)
    t = max(0, min(1, t))
    t_a = parse_iso(p_a["ts_utc"])
    t_b = parse_iso(p_b["ts_utc"])
    departure = t_a + timedelta(seconds=(t_b - t_a).total_seconds() * t)
    return departure.isoformat()


def estimate_arrival_time(trip_pings: list[dict],
                           terminus_lat: float | None,
                           terminus_lng: float | None) -> str:
    """
    Estimate when the bus actually arrived at the terminus.
    Finds the first ping near the terminus and interpolates
    between the previous ping and that ping.
    Falls back to last ping timestamp if terminus coords unavailable.
    """
    if terminus_lat is None or terminus_lng is None or len(trip_pings) < 2:
        return trip_pings[-1]["ts_utc"]

    # Find first ping close to terminus
    for i in range(1, len(trip_pings)):
        dist = haversine_m(
            trip_pings[i]["lat"], trip_pings[i]["lng"],
            terminus_lat, terminus_lng
        )
        if dist <= MAX_MATCH_DISTANCE_M:
            # Interpolate between previous ping and this one
            p_a = trip_pings[i-1]
            p_b = trip_pings[i]
            dist_a = haversine_m(p_a["lat"], p_a["lng"], terminus_lat, terminus_lng)
            dist_b = dist

            if dist_a <= dist_b:
                return p_b["ts_utc"]

            # t = proportion where distance crosses threshold
            t = (dist_a - MAX_MATCH_DISTANCE_M) / (dist_a - dist_b)
            t = max(0, min(1, t))
            t_a = parse_iso(p_a["ts_utc"])
            t_b = parse_iso(p_b["ts_utc"])
            arrival = t_a + timedelta(seconds=(t_b - t_a).total_seconds() * t)
            return arrival.isoformat()

    return trip_pings[-1]["ts_utc"]


def is_valid_trip(trip_pings: list[dict], stops: list[dict],
                  started_at: str, ended_at: str) -> bool:
    """
    Returns True only if this ping sequence represents a real completed trip.

    The key check is ROUTE-PROGRESS SPAN: the bus must have physically
    traversed at least MIN_PROGRESS_SPAN (50%) of the route from its lowest
    to highest progress point. This kills fragments like a bus caught only
    at the tail end of a route (high stop-coverage but tiny span) and the
    sparse-polling "9-minute trip" artifact, while still accepting real trips
    even if we missed the exact endpoints.
    """
    # Absolute duration floor
    try:
        duration_mins = (parse_iso(ended_at) - parse_iso(started_at)).total_seconds()/60
        if duration_mins < MIN_TRIP_DURATION_MINS:
            return False
    except Exception:
        return False

    # Movement floor
    if len(trip_pings) >= 2:
        max_dist = 0.0
        first = trip_pings[0]
        for p in trip_pings[1:]:
            max_dist = max(max_dist, haversine_m(
                first["lat"], first["lng"], p["lat"], p["lng"]))
        if max_dist < MIN_MOVEMENT_M:
            return False

    # Route-progress span — the decisive check
    if stops and len(stops) >= 4:
        progresses = []
        for p in trip_pings:
            prog, dist = compute_progress(p, stops)
            if prog is not None and dist is not None and dist <= MAX_MATCH_DISTANCE_M * 2:
                progresses.append(prog)
        if not progresses:
            return False
        span = max(progresses) - min(progresses)
        if span < MIN_PROGRESS_SPAN:
            return False

    return True


def _interpolate_pass_time(p_a, p_b, stop_lat, stop_lng):
    lat_ref = math.radians(p_a["lat"])
    mpd_lat = 111320.0
    mpd_lng = 111320.0 * math.cos(lat_ref)

    bx = (p_b["lng"] - p_a["lng"]) * mpd_lng
    by = (p_b["lat"] - p_a["lat"]) * mpd_lat
    sx = (stop_lng   - p_a["lng"]) * mpd_lng
    sy = (stop_lat   - p_a["lat"]) * mpd_lat

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
                        departure_time: str | None = None,
                        arrival_time: str | None = None) -> list[dict]:
    """
    Match stops to trip using interpolation + snap fallback.
    Override first/last stop times with precise departure/arrival if available.
    """
    if not trip_pings or not stops:
        return []

    results: dict[int, dict] = {}
    matched_orders = set()
    stop_cursor = 0

    # Pass 1: interpolation between ping pairs
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
        while (stop_cursor < len(stops) and
               stops[stop_cursor]["stop_order"] in matched_orders):
            stop_cursor += 1

    # Pass 2: snap fallback for unmatched stops
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

    # Pass 3: override terminus stops with precise times
    if results:
        first_order = min(results.keys())
        last_order  = max(results.keys())
        if departure_time and first_order in results:
            results[first_order]["passed_at"] = departure_time
            results[first_order]["method"]    = "terminus_observed"
            results[first_order]["distance_m"] = 0.0
        if arrival_time and last_order in results:
            results[last_order]["passed_at"] = arrival_time
            results[last_order]["method"]    = "terminus_observed"
            results[last_order]["distance_m"] = 0.0

    return sorted(results.values(), key=lambda x: x["stop_order"])


def get_terminus_observed_times(conn, route_code, vehicle_no,
                                trip_start, trip_end):
    window_start = (parse_iso(trip_start) - timedelta(minutes=10)).isoformat()
    window_end   = (parse_iso(trip_end)   + timedelta(minutes=10)).isoformat()

    origin_stop = conn.execute("""
        SELECT stop_code, lat, lng FROM stops
        WHERE route_code=? AND stop_order=(SELECT MIN(stop_order) FROM stops WHERE route_code=?)
    """, (route_code, route_code)).fetchone()

    terminus_stop = conn.execute("""
        SELECT stop_code, lat, lng FROM stops
        WHERE route_code=? AND stop_order=(SELECT MAX(stop_order) FROM stops WHERE route_code=?)
    """, (route_code, route_code)).fetchone()

    origin_time = None
    if origin_stop:
        row = conn.execute("""
            SELECT observed_at, predicted_mins FROM terminus_observations
            WHERE route_code=? AND stop_code=? AND stop_type='origin'
              AND (vehicle_no=? OR vehicle_no='')
              AND observed_at BETWEEN ? AND ?
            ORDER BY ABS(predicted_mins) ASC LIMIT 1
        """, (route_code, origin_stop["stop_code"], vehicle_no,
              window_start, trip_start)).fetchone()
        if row:
            try:
                actual = parse_iso(row["observed_at"]) + timedelta(minutes=row["predicted_mins"])
                origin_time = actual.isoformat()
            except Exception:
                pass

    terminus_time = None
    if terminus_stop:
        row = conn.execute("""
            SELECT observed_at, predicted_mins FROM terminus_observations
            WHERE route_code=? AND stop_code=? AND stop_type='terminus'
              AND (vehicle_no=? OR vehicle_no='')
              AND observed_at BETWEEN ? AND ?
            ORDER BY ABS(predicted_mins) ASC LIMIT 1
        """, (route_code, terminus_stop["stop_code"], vehicle_no,
              trip_end, window_end)).fetchone()
        if row:
            try:
                actual = parse_iso(row["observed_at"]) + timedelta(minutes=row["predicted_mins"])
                terminus_time = actual.isoformat()
            except Exception:
                pass

    return (origin_time,
            terminus_time,
            (origin_stop["lat"], origin_stop["lng"]) if origin_stop else (None, None),
            (terminus_stop["lat"], terminus_stop["lng"]) if terminus_stop else (None, None))


def reconstruct_route_day(conn, route_code: str, service_date: str,
                          computed_at: str) -> dict:
    """Reconstruct trips for one route on one service_date. Idempotent."""
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM trips WHERE route_code=? AND service_date=?",
        (route_code, service_date)
    ).fetchall()]
    if old_ids:
        ph = ",".join("?"*len(old_ids))
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
        for trip_pings in segment_into_trips(pings, stops):
            if len(trip_pings) < 2:
                continue

            raw_start = trip_pings[0]["ts_utc"]
            raw_end   = trip_pings[-1]["ts_utc"]

            # Get precise terminus times and stop coordinates
            (obs_origin, obs_terminus,
             (origin_lat, origin_lng),
             (term_lat, term_lng)) = get_terminus_observed_times(
                conn, route_code, vehicle_no, raw_start, raw_end
            )

            # Estimate precise departure/arrival via GPS interpolation
            # (used if no getStopArrivals data available)
            gps_departure = estimate_departure_time(
                trip_pings, origin_lat, origin_lng
            )
            gps_arrival = estimate_arrival_time(
                trip_pings, term_lat, term_lng
            )

            # Progress-rate extrapolation: estimates endpoints from the bus's
            # own observed speed. Critical for sparse data where the bus was
            # only caught mid-route and never seen at the origin/terminus.
            extrap_departure, extrap_arrival = estimate_endpoints_by_progress(
                trip_pings, stops
            )

            # Was the bus actually seen near the origin / terminus?
            first_prog, _ = compute_progress(trip_pings[0], stops) if stops else (None, None)
            last_prog, _  = compute_progress(trip_pings[-1], stops) if stops else (None, None)
            seen_near_origin   = first_prog is not None and first_prog <= 0.15
            seen_near_terminus = last_prog  is not None and last_prog  >= 0.85

            # Priority for DEPARTURE:
            #   1. getStopArrivals observation (most accurate)
            #   2. GPS interpolation IF bus was seen near origin
            #   3. progress extrapolation (bus caught mid-route)
            #   4. raw first ping
            if obs_origin:
                started_at = obs_origin
            elif seen_near_origin:
                started_at = gps_departure
            elif extrap_departure:
                started_at = extrap_departure
            else:
                started_at = gps_departure

            # Priority for ARRIVAL (same logic, mirrored)
            if obs_terminus:
                terminus_arrived = obs_terminus
            elif seen_near_terminus:
                terminus_arrived = gps_arrival
            elif extrap_arrival:
                terminus_arrived = extrap_arrival
            else:
                terminus_arrived = gps_arrival

            # Validate this is a real trip
            if not is_valid_trip(trip_pings, stops, started_at, terminus_arrived):
                continue

            matches = match_stops_to_trip(
                trip_pings, stops, started_at, terminus_arrived
            ) if stops else []

            cur = conn.execute("""
                INSERT INTO trips
                    (route_code, vehicle_no, service_date, started_at, ended_at,
                     terminus_arrived_at, stop_count, computed_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (route_code, vehicle_no, service_date,
                  started_at, terminus_arrived,
                  terminus_arrived, len(matches), computed_at))
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
                        (vehicle_no, route_code, service_date,
                         departed_at, trip_id, computed_at)
                    VALUES (?,?,?,?,?,?)
                """, (vehicle_no, route_code, service_date,
                      started_at, trip_id, computed_at))
                n_departures += 1

    return {
        "route_code":        route_code,
        "trips":             n_trips,
        "departures":        n_departures,
        "distinct_vehicles": len(distinct_vehicles),
    }
