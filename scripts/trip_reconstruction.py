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
                                   stops: list[dict],
                                   route_duration_mins: float | None = None
                                   ) -> tuple[str | None, str | None]:
    """
    Estimate origin-departure and terminus-arrival times for a bus caught
    mid-route.

    Two strategies, in order of robustness:

    A) ANCHORED (preferred): if a typical route_duration_mins is known (median
       of completed trips on this route), anchor the estimate on it. A bus seen
       at progress P at time T departed roughly  T - P * duration  and will
       arrive roughly  T + (1 - P) * duration. Uses the MEDIAN progress/time of
       all valid pings as the anchor point, which averages out GPS noise.
       This is far more stable than estimating speed from sparse, close pings.

    B) SELF-RATE (fallback): if no route duration is known, fit the bus's own
       progress-per-minute via least squares and extrapolate. Works but is
       noisy when pings are few and close together.

    Returns (departure_iso, arrival_iso) or (None, None).
    """
    if not stops or len(stops) < 4 or len(trip_pings) < 1:
        return None, None

    pts = []  # (datetime, progress)
    for p in trip_pings:
        prog, dist = compute_progress(p, stops)
        if prog is not None and dist is not None and dist <= MAX_MATCH_DISTANCE_M * 2:
            pts.append((parse_iso(p["ts_utc"]), prog))

    if not pts:
        return None, None

    # ── Strategy A: anchored on known route duration ──
    if route_duration_mins and route_duration_mins > 0:
        # For EACH ping compute the implied departure (time - progress*duration)
        # and implied arrival (time + (1-progress)*duration), then take the
        # MEDIAN. Using all pings averages out GPS noise far better than a
        # single anchor point.
        implied_dep = []
        implied_arr = []
        for t, pr in pts:
            implied_dep.append(t - timedelta(minutes=pr * route_duration_mins))
            implied_arr.append(t + timedelta(minutes=(1 - pr) * route_duration_mins))
        implied_dep.sort()
        implied_arr.sort()
        dep = implied_dep[len(implied_dep)//2]
        arr = implied_arr[len(implied_arr)//2]
        return dep.isoformat(), arr.isoformat()

    # ── Strategy B: self-rate least squares (needs >=2 spread points) ──
    if len(pts) < 2:
        return None, None
    t0 = pts[0][0]
    xy = [((t - t0).total_seconds()/60, pr) for t, pr in pts]
    n = len(xy)
    sx = sum(x for x, _ in xy); sy = sum(y for _, y in xy)
    sxy = sum(x*y for x, y in xy); sxx = sum(x*x for x, _ in xy)
    denom = n*sxx - sx*sx
    if abs(denom) < 1e-9:
        return None, None
    b = (n*sxy - sx*sy) / denom    # progress per minute
    a = (sy - b*sx) / n
    if b <= 1e-6:
        return None, None
    departure = (t0 + timedelta(minutes=(0.0 - a)/b)).isoformat()
    arrival   = (t0 + timedelta(minutes=(1.0 - a)/b)).isoformat()
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


def classify_trip(trip_pings: list[dict], stops: list[dict],
                  started_at: str, ended_at: str) -> str:
    """
    Classify a ping sequence as one of:
      "complete"   — a real trip that reached (near) the terminus
      "incomplete" — a bus that departed normally from the origin but was
                     never seen reaching the terminus (went out of service,
                     lost signal, etc). Recorded with departure but no arrival.
      "reject"     — a GPS fragment / stationary bus / sparse-poll artifact

    Logic:
      started_near_origin  = first ping at progress <= 0.20
      reached_terminus     = max progress >= 0.85
      span                 = max - min progress

      complete   : started_near_origin AND reached_terminus
                   OR (caught mid-route but span >= MIN_PROGRESS_SPAN)
      incomplete : started_near_origin AND NOT reached_terminus AND moved
                   forward meaningfully (max progress >= 0.25)
      reject     : everything else
    """
    # Absolute duration & movement floors (apply to all)
    try:
        duration_mins = (parse_iso(ended_at) - parse_iso(started_at)).total_seconds()/60
    except Exception:
        return "reject"

    if len(trip_pings) >= 2:
        max_dist = 0.0
        first = trip_pings[0]
        for p in trip_pings[1:]:
            max_dist = max(max_dist, haversine_m(
                first["lat"], first["lng"], p["lat"], p["lng"]))
        if max_dist < MIN_MOVEMENT_M:
            return "reject"

    # Without stop geometry we can only do a basic duration check
    if not stops or len(stops) < 4:
        return "complete" if duration_mins >= MIN_TRIP_DURATION_MINS else "reject"

    progresses = []
    for p in trip_pings:
        prog, dist = compute_progress(p, stops)
        if prog is not None and dist is not None and dist <= MAX_MATCH_DISTANCE_M * 2:
            progresses.append(prog)
    if not progresses:
        return "reject"

    first_progress = progresses[0]
    max_progress   = max(progresses)
    span           = max_progress - min(progresses)

    started_near_origin = first_progress <= 0.20
    reached_terminus    = max_progress >= 0.85

    # Reaching the terminus is strong evidence of a completed trip, regardless
    # of where we first caught the bus. With a known route duration we can
    # back-calculate the departure accurately, so accept it as complete.
    if reached_terminus:
        return "complete" if duration_mins >= MIN_TRIP_DURATION_MINS else "reject"

    # Caught a large middle chunk (didn't see terminus but covered a lot)
    if not started_near_origin and span >= MIN_PROGRESS_SPAN:
        return "complete" if duration_mins >= MIN_TRIP_DURATION_MINS else "reject"

    # Departed origin normally but never reached terminus → incomplete
    if started_near_origin and max_progress >= 0.25:
        return "incomplete"

    return "reject"


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

    # Pass 4: timestamp sanitizer (adapted from fragkakis TripTimestampSanitizer).
    # Stop pass-times must increase monotonically along the route. Sparse polling
    # and snapping can produce small out-of-order times; re-sort the timestamps
    # so they match the physical stop order.
    ordered = sorted(results.values(), key=lambda x: x["stop_order"])
    times = sorted(r["passed_at"] for r in ordered)
    for r, t in zip(ordered, times):
        r["passed_at"] = t

    return ordered


def get_terminus_observed_times(conn, route_code, vehicle_no,
                                trip_start, trip_end, route_duration_mins=None):
    """
    Determine exact departure and arrival times, preferring disappearance-
    detected stop passages.

    Departure: the origin gives no arrival prediction on non-circular routes,
    so we take the EARLIEST exact passage at any tracked near-origin stop and
    back-calculate to the origin using that stop's route progress:
        departure = pass_time - progress(stop) * route_duration
    Since near-origin stops have tiny progress, this is highly accurate.

    Arrival: the LATEST exact passage at a near-terminus stop, extrapolated
    forward to the terminus (or used directly if it IS the terminus).
    """
    window_start = (parse_iso(trip_start) - timedelta(minutes=20)).isoformat()
    window_end   = (parse_iso(trip_end)   + timedelta(minutes=20)).isoformat()

    bounds = conn.execute("""
        SELECT MIN(stop_order) lo, MAX(stop_order) hi FROM stops WHERE route_code=?
    """, (route_code,)).fetchone()
    lo_order = bounds["lo"] if bounds else None
    hi_order = bounds["hi"] if bounds else None
    span = (hi_order - lo_order) if (lo_order is not None and hi_order is not None and hi_order > lo_order) else None

    origin_stop = conn.execute("""
        SELECT lat, lng FROM stops WHERE route_code=? AND stop_order=?
    """, (route_code, lo_order)).fetchone() if lo_order is not None else None
    terminus_stop = conn.execute("""
        SELECT lat, lng FROM stops WHERE route_code=? AND stop_order=?
    """, (route_code, hi_order)).fetchone() if hi_order is not None else None

    def progress_of(order):
        if span and order is not None:
            return (order - lo_order) / span
        return None

    # All exact passages for this vehicle within the trip window
    passages = conn.execute("""
        SELECT stop_type, stop_order, passed_at FROM stop_passages
        WHERE route_code=? AND vehicle_no=? AND passed_at BETWEEN ? AND ?
        ORDER BY passed_at
    """, (route_code, vehicle_no, window_start, window_end)).fetchall()

    origin_time = terminus_time = None

    if passages:
        # EARLIEST passage → back-calculate departure
        earliest = passages[0]
        p = progress_of(earliest["stop_order"])
        if p is not None and route_duration_mins:
            origin_time = (parse_iso(earliest["passed_at"]) -
                           timedelta(minutes=p * route_duration_mins)).isoformat()
        elif earliest["stop_type"] == "origin":
            origin_time = earliest["passed_at"]

        # LATEST passage → arrival
        latest = passages[-1]
        if latest["stop_type"] == "terminus":
            terminus_time = latest["passed_at"]
        else:
            p = progress_of(latest["stop_order"])
            if p is not None and route_duration_mins:
                terminus_time = (parse_iso(latest["passed_at"]) +
                                 timedelta(minutes=(1 - p) * route_duration_mins)).isoformat()

    # Fallback to legacy terminus_observations if no passages
    if origin_time is None:
        o = conn.execute("""
            SELECT observed_at, predicted_mins FROM terminus_observations
            WHERE route_code=? AND stop_type='origin' AND (vehicle_no=? OR vehicle_no='')
              AND observed_at BETWEEN ? AND ?
            ORDER BY ABS(predicted_mins) ASC LIMIT 1
        """, (route_code, vehicle_no, window_start, trip_start)).fetchone()
        if o:
            try:
                origin_time = (parse_iso(o["observed_at"]) +
                               timedelta(minutes=o["predicted_mins"])).isoformat()
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

    # ── First pass: estimate the route's typical trip duration ──
    # Use segments that were caught spanning most of the route (near origin to
    # near terminus), so their first→last ping time is a good duration sample.
    duration_samples = []
    for vehicle_no, pings in by_vehicle.items():
        for trip_pings in segment_into_trips(pings, stops):
            if len(trip_pings) < 2 or not stops or len(stops) < 4:
                continue
            progs = []
            for p in trip_pings:
                pr, d = compute_progress(p, stops)
                if pr is not None and d is not None and d <= MAX_MATCH_DISTANCE_M * 2:
                    progs.append((pr, p["ts_utc"]))
            if len(progs) < 2:
                continue
            lo_prog = min(pr for pr, _ in progs)
            hi_prog = max(pr for pr, _ in progs)
            span = hi_prog - lo_prog
            if span >= 0.6:  # covered most of the route → reliable duration
                t_first = parse_iso(trip_pings[0]["ts_utc"])
                t_last  = parse_iso(trip_pings[-1]["ts_utc"])
                observed = (t_last - t_first).total_seconds() / 60
                if span > 0 and observed > 0:
                    # Scale observed span-time up to a full 0→1 trip
                    full = observed / span
                    if 5 < full < 180:
                        duration_samples.append(full)

    # Today's duration estimate (median of well-covered trips today)
    today_duration = None
    if duration_samples:
        duration_samples.sort()
        today_duration = duration_samples[len(duration_samples)//2]

    # Persistent historical duration (accumulated across previous days).
    # This is what makes departure estimation work even on sparse days.
    persistent_duration = None
    try:
        row = conn.execute(
            "SELECT median_trip_duration_mins FROM route_rotation WHERE route_code=?",
            (route_code,)
        ).fetchone()
        if row and row["median_trip_duration_mins"]:
            persistent_duration = row["median_trip_duration_mins"]
    except Exception:
        pass

    # Prefer today's estimate when we have several samples; otherwise lean on
    # the stable historical value. With one source, use whichever exists.
    if today_duration and len(duration_samples) >= 3:
        route_duration = today_duration
    elif persistent_duration:
        route_duration = persistent_duration
    else:
        route_duration = today_duration

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
                conn, route_code, vehicle_no, raw_start, raw_end, route_duration
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
                trip_pings, stops, route_duration
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

            # Classify: complete / incomplete / reject
            status = classify_trip(trip_pings, stops, started_at, terminus_arrived)
            if status == "reject":
                continue

            # Incomplete trips: bus departed but never reached terminus.
            # Record the departure, but arrival/duration are unknown (NULL).
            if status == "incomplete":
                terminus_arrived = None

            matches = match_stops_to_trip(
                trip_pings, stops,
                started_at,
                terminus_arrived if status == "complete" else None
            ) if stops else []

            # ended_at always = last observed ping (satisfies NOT NULL).
            # terminus_arrived_at = arrival for complete trips, NULL for incomplete.
            ended_at_val   = raw_end
            terminus_val   = terminus_arrived if status == "complete" else None

            cur = conn.execute("""
                INSERT INTO trips
                    (route_code, vehicle_no, service_date, started_at, ended_at,
                     terminus_arrived_at, stop_count, computed_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (route_code, vehicle_no, service_date,
                  started_at, ended_at_val, terminus_val,
                  len(matches), computed_at))
            trip_id = cur.lastrowid
            n_trips += 1

            for m in matches:
                conn.execute("""
                    INSERT INTO trip_stop_times
                        (trip_id, stop_code, stop_order, passed_at, distance_m, method)
                    VALUES (?,?,?,?,?,?)
                """, (trip_id, m["stop_code"], m["stop_order"],
                      m["passed_at"], m["distance_m"], m["method"]))

            # Record the departure for slot assignment (both complete & incomplete)
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
