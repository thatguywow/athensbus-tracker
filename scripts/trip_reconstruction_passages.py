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

LOOP_TERMINAL_METRES = 300   # first/last stop this close ⇒ loop route
LINFIT_MIN_SPAN = 4     # εύρος στάσεων ώστε η παλινδρόμηση να προτιμηθεί έναντι μαθημένου τμήματος
MIN_TRIP_GAP_MINUTES = 20    # #3: κάτω όριο για το κενό που σπάει δρομολόγιο
MAX_BOUNDARY_ZONE_MINS = 45  # #6: πλαφόν ζώνης συνόρων (μισό headway, έως 45′)
LOOP_DWELL_MINS = 3.0        # κενό στην αφετηρία κυκλικής πάνω από αυτό ⇒ στάθμευση, όχι αναχώρηση
LOOP_MIN_DURATION_FRACTION = 0.7   # Ο κανόνας στάθμευσης ανατρέπεται όταν δίνει
                                   # διάρκεια < 70% της ΜΑΘΗΜΕΝΗΣ τυπικής. Απαιτεί
                                   # ΣΩΣΤΗ διάμεσο: όσο η route_rotation είχε μάθει
                                   # διάρκειες ΜΕ τη στάθμευση μέσα (118′ αντί ~90′),
                                   # το δίχτυ τράβαγε γνήσιες βόλτες προς το παλιό
                                   # φουσκωμένο νούμερο. Αν λείπει η διάμεσος (μετά
                                   # από μηδενισμό), το δίχτυ ΔΕΝ ενεργεί — οι
                                   # διάρκειες μένουν όπως μετρήθηκαν.
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


def _stop_distances(conn, route_code: str) -> dict:
    """
    #15 DIST: cumulative metres along the route for each stop_order.

    Interpolating on stop ORDER assumes every interval is equally long. In
    reality two stops can be 200 m apart downtown and 2 km apart on an avenue,
    so an order-based fit or pace mis-places the projection badly on routes with
    uneven spacing. Straight-line distance between consecutive stops is a much
    better x-axis, and the coordinates are already in our stops table.
    """
    rows = conn.execute(
        "SELECT stop_order, lat, lng FROM stops WHERE route_code=? "
        "ORDER BY stop_order", (route_code,)).fetchall()
    out, total, prev = {}, 0.0, None
    for r in rows:
        if r["lat"] is None or r["lng"] is None:
            return {}                       # incomplete geometry ⇒ fall back
        if prev is not None:
            dx = (float(r["lat"]) - prev[0]) * 111_000.0
            dy = (float(r["lng"]) - prev[1]) * 88_000.0
            total += (dx * dx + dy * dy) ** 0.5
        out[r["stop_order"]] = total
        prev = (float(r["lat"]), float(r["lng"]))
    return out if total > 0 else {}


def _linfit_predict(points: list[tuple[int, datetime]], x_target: int,
                    xmap: dict | None = None):
    """
    Least-squares fit of time (seconds) vs stop_order, predict time at x_target.
    points: list of (stop_order, datetime). Needs >=2 distinct stop_orders.
    Returns a datetime or None.
    """
    # With xmap the fit runs on DISTANCE (metres) instead of stop index.
    def _x(o):
        return xmap.get(o, o) if xmap else o

    xs = [_x(p[0]) for p in points]
    if len(set(xs)) < 2:
        return None
    t0 = min(p[1] for p in points)
    xy = [(_x(x), (t - t0).total_seconds()) for x, t in points]
    n = len(xy)
    sx = sum(x for x, _ in xy)
    sy = sum(y for _, y in xy)
    sxy = sum(x*y for x, y in xy)
    sxx = sum(x*x for x, _ in xy)
    denom = n*sxx - sx*sx
    if abs(denom) < 1e-9:
        return None
    b = (n*sxy - sx*sy) / denom      # seconds per unit of x (stop index or metre)
    a = (sy - b*sx) / n
    return t0 + timedelta(seconds=a + b*_x(x_target))


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
    """
    Weighted distance of departure t to the nearest scheduled slot. A LATE
    departure (delay) is normal; leaving EARLIER than a slot is rare — so
    earliness costs double. This stops junk early-morning slots in today's
    feed (e.g. a stray 04:15) from stealing yesterday's delayed 03:55 trip.
    """
    if not dts:
        return None
    best = None
    for d in dts:
        diff = (t - d).total_seconds()
        cost = diff if diff >= 0 else -diff * 2.0
        if best is None or cost < best:
            best = cost
    return best


def _boundary_zone_mins(conn, route_code: str, sched_date: str) -> float:
    """
    #6: how far from 04:00 the ownership arbitration still applies.

    A flat ±20′ suits a 10-minute headway but is far too tight for night or
    suburban services: with a 60′ headway, a bus leaving 03:40 plainly belongs
    to yesterday's 03:55 slot, yet it fell outside the zone and was judged by
    clock time alone. The zone now scales with the route's own headway.
    """
    dts = _sched_datetimes(conn, route_code, sched_date)
    if len(dts) < 3:
        return BOUNDARY_ZONE_MINS
    times = sorted(dts)
    gaps = [(times[i + 1] - times[i]).total_seconds() / 60
            for i in range(len(times) - 1)]
    gaps = [g for g in gaps if 0 < g <= 240]
    if not gaps:
        return BOUNDARY_ZONE_MINS
    import statistics as _st
    headway = _st.median(gaps)
    return min(max(BOUNDARY_ZONE_MINS, headway * 0.5), MAX_BOUNDARY_ZONE_MINS)


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


def _split_trips(passages: list[dict], route_duration: float | None,
                 loop_mid: float | None = None) -> list[list[dict]]:
    """
    Chain one vehicle's passages (already sorted by passed_at) into trips.

    Edge-only tracking means the middle of the route is unobserved, so the time
    gap between the origin-side cluster and the terminus-side cluster is normally
    large (the whole traversal). Therefore we split a new trip when stop_order
    does NOT advance (a reset back toward the origin), and only use a time gap as
    a secondary guard when it exceeds a generous span (≈1.5× the route duration).

    loop_mid: set to the route midpoint for LOOP routes (first/last stop at the
    same physical terminal). There, arrival passages and the next departure's
    origin passage INTERLEAVE in time (…40 → 1 → 41…), which naively produces
    45-second "laps" and back-to-back ghosts. Two loop rules fix this:
      • straggler routing: a terminus-side point that lands right after a fresh
        origin cluster, within TERMINAL_CLUSTER of the previous trip's last
        terminus point, belongs to the PREVIOUS trip (it is its arrival tail);
      • dwell ghosts: an origin-only micro-cluster recorded seconds after an
        arrival (vehicle parked at the terminal) is noise — dropped.
    """
    # #3 ADAPTIVE GAP: with edge-only tracking the gap between the origin and
    # terminus clusters IS the traversal, so the limit must exceed the route's
    # own duration — 1.5× gives slack for a congested day. The old hard floor of
    # 60′ made short routes far too permissive (a 10-minute route tolerated six
    # consecutive trips as one); the floor is now MIN_TRIP_GAP_MINUTES.
    gap_limit = (route_duration * 1.5) if route_duration else 90.0
    gap_limit = max(gap_limit, MIN_TRIP_GAP_MINUTES)

    JITTER_MINS = 3.0        # tiny regressions within this window are noise…
    ORDER_JITTER = 3         # …if the order drop stays inside the edge cluster
    TERMINAL_CLUSTER = 8.0   # minutes: arrival stragglers / dwell-ghost window
    young_limit = (route_duration * 0.5) if route_duration else 20.0
    mature_mins = (route_duration * 0.5) if route_duration else 20.0

    trips: list[list[dict]] = []
    cur: list[dict] = []
    for p in passages:
        if not cur:
            cur = [p]
            continue
        prev = cur[-1]
        pdt = _parse(p["passed_at"])
        gap = (pdt - _parse(prev["passed_at"])).total_seconds() / 60
        regressed = p["stop_order"] <= prev["stop_order"]
        jitter = (prev["stop_order"] - p["stop_order"]) <= ORDER_JITTER and gap <= JITTER_MINS

        # LOOP: a terminus-side point arriving while `cur` is still a young
        # origin-only cluster cannot belong to `cur` (it could not possibly
        # have crossed the route yet) — it closes the PREVIOUS lap.
        if (loop_mid is not None and trips
                and p["stop_order"] > loop_mid
                and all(q["stop_order"] <= loop_mid for q in cur)
                and (pdt - _parse(cur[0]["passed_at"])).total_seconds() / 60 < young_limit):
            prev_trip = trips[-1]
            prev_last = prev_trip[-1]
            prev_has_arrival = any(q["stop_order"] > loop_mid for q in prev_trip)
            attach = False
            if (prev_last["stop_order"] > loop_mid
                    and (pdt - _parse(prev_last["passed_at"])).total_seconds() / 60
                        <= TERMINAL_CLUSTER):
                attach = True    # (a) straggler of an arrival already under way
            elif (not prev_has_arrival
                  and (pdt - _parse(prev_trip[0]["passed_at"])).total_seconds() / 60
                      >= mature_mins):
                # (b) the previous lap left the origin long enough ago and never
                # registered an arrival — this IS its arrival. Without this, the
                # lap stays open ("—") and the fresh origin point plus these
                # terminus points fake a one-minute lap.
                attach = True
            if attach:
                prev_trip.append(p)
                continue

        if (regressed and not jitter) or gap > gap_limit:
            trips.append(cur)
            cur = [p]
        else:
            cur.append(p)
    if cur:
        trips.append(cur)

    # LOOP dwell ghosts: origin-only micro-cluster seconds after an arrival
    # (the vehicle is parked at the shared terminal, not departing).
    if loop_mid is not None:
        kept = []
        for i, t in enumerate(trips):
            if (i > 0 and len(t) <= 2
                    and all(q["stop_order"] <= loop_mid for q in t)
                    and (_parse(t[-1]["passed_at"]) - _parse(t[0]["passed_at"]))
                        .total_seconds() / 60 <= JITTER_MINS):
                prev_last = kept[-1][-1] if kept else None
                delta = ((_parse(t[0]["passed_at"]) - _parse(prev_last["passed_at"]))
                         .total_seconds() / 60) if prev_last is not None else None
                # Must come AFTER the arrival: a cluster recorded BEFORE the
                # previous lap's (late-registered) arrival is a real departure,
                # not parking noise — hence delta >= 0.
                if (prev_last is not None and prev_last["stop_order"] > loop_mid
                        and delta is not None and 0 <= delta <= TERMINAL_CLUSTER):
                    continue   # parked-at-terminal noise → drop
            kept.append(t)
        trips = kept
    return trips


def passage_query_window(service_date: str) -> tuple[str, str]:
    """
    The exact passage window this module reads for one service day (UTC ISO).
    Exposed so incremental compute can detect changes over the SAME range —
    if the window ever changes, detection follows automatically.
    """
    start_bound, end_bound = _athens_window(service_date)
    day_start, day_end = _parse(start_bound), _parse(end_bound)
    return ((day_start - timedelta(minutes=BOUNDARY_ZONE_MINS + 10)).isoformat(),
            (day_end + timedelta(hours=OVERLAP_HOURS)).isoformat())


def boundary_zone_windows(service_date: str) -> list[tuple[str, str]]:
    """
    The two ±BOUNDARY_ZONE windows (day start and day end) where a trip's
    ownership can depend on the NEIGHBOURING day's schedule. Only passages
    inside these windows make a neighbour-schedule change relevant.
    """
    start_bound, end_bound = _athens_window(service_date)
    ds, de = _parse(start_bound), _parse(end_bound)
    bz = timedelta(minutes=BOUNDARY_ZONE_MINS + 10)
    return [((ds - bz).isoformat(), (ds + bz).isoformat()),
            ((de - bz).isoformat(), (de + bz).isoformat())]


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

    # LOOP ROUTE DETECTION: some routes (e.g. 619) end at the same terminal
    # they start from — not necessarily the same stop_code, but a neighbouring
    # stop of the same terminal loop. Arrival and next-departure passages then
    # interleave in time, which the splitter must handle specially.
    # TERMINAL-ANCHORED ORIGIN: if this route's origin stop is an OASA terminal
    # (authoritative `isTerminal`, synced by sync_master_data), the passage seen
    # there is the TERMINAL EVENT — a bus arriving from its previous lap, which
    # on a shared terminal may even be the sibling direction. The dwell logic
    # below then applies: that passage is an arrival, and the real departure
    # comes from the later origin-side passages. This covers loop routes AND
    # bidirectional pairs sharing a terminal, with no cross-route lookups.
    # LEARNED SEGMENTS: median observed travel time origin→stop for this route,
    # collected by rotation_slots but never read until now. Back-calculating a
    # departure with a UNIFORM pace systematically underestimates the first few
    # stops (boarding, city-centre traffic), which pushed departures too late
    # and made durations look shorter than reality — visible on 421 inbound.
    distances = _stop_distances(conn, route_code)
    segments = {r["stop_order"]: r["median_mins"] for r in conn.execute(
        "SELECT stop_order, median_mins FROM segment_times "
        "WHERE route_code=? AND median_mins IS NOT NULL", (route_code,))}

    loop_mid = None
    term_row = conn.execute("""
        SELECT t.terminal_id FROM stops s
        JOIN stop_terminals t ON t.stop_code = s.stop_code
        WHERE s.route_code=? AND s.stop_order=?""", (route_code, lo)).fetchone()
    if term_row and term_row["terminal_id"]:
        loop_mid = (lo + hi) / 2.0
    else:
        # Fallback while terminals are not yet synced: same stop code at both
        # ends, or ends within LOOP_TERMINAL_METRES of each other.
        ends = conn.execute(
            "SELECT stop_order, stop_code, lat, lng FROM stops "
            "WHERE route_code=? AND stop_order IN (?, ?)",
            (route_code, lo, hi)).fetchall()
        if len(ends) == 2:
            a, b = ends[0], ends[1]
            same = a["stop_code"] == b["stop_code"]
            if not same and None not in (a["lat"], a["lng"], b["lat"], b["lng"]):
                dx = (float(a["lat"]) - float(b["lat"])) * 111_000.0
                dy = (float(a["lng"]) - float(b["lng"])) * 88_000.0
                same = (dx * dx + dy * dy) ** 0.5 <= LOOP_TERMINAL_METRES
            if same:
                loop_mid = (lo + hi) / 2.0
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
    query_start, query_end = passage_query_window(service_date)
    rows = conn.execute("""
        SELECT vehicle_no, stop_code, stop_order, passed_at
        FROM stop_passages
        WHERE route_code=? AND passed_at>=? AND passed_at<?
        -- Tie-break on stop_order DESC: on a loop route the arrival (order N)
        -- and the next lap's presence at the same physical stop (order 1) are
        -- written with the SAME timestamp. Reading the arrival first lets it
        -- close the finished lap, instead of both landing in a new one.
        ORDER BY vehicle_no, passed_at, stop_order DESC
    """, (route_code, query_start, query_end)).fetchall()

    by_vehicle: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["stop_order"] is None:
            continue
        by_vehicle[r["vehicle_no"]].append(dict(r))

    n_trips = n_departures = 0
    distinct_vehicles = set()

    for vehicle_no, plist in by_vehicle.items():
        vehicle_trips = _split_trips(plist, route_duration, loop_mid)
        for _ti, trip in enumerate(vehicle_trips):
            if not trip:
                continue
            # First passage of this vehicle's NEXT lap — an ESTIMATED arrival
            # may never postdate it (the bus cannot still be arriving after it
            # has been seen starting again).
            next_first_dt = (_parse(vehicle_trips[_ti + 1][0]["passed_at"])
                             if _ti + 1 < len(vehicle_trips) else None)

            origin_side = [(p["stop_order"], _parse(p["passed_at"]))
                           for p in trip if p["stop_order"] <= mid]
            term_side = [(p["stop_order"], _parse(p["passed_at"]))
                         for p in trip if p["stop_order"] > mid]
            first_dt = _parse(trip[0]["passed_at"])
            last_dt = _parse(trip[-1]["passed_at"])

            # ── DEPARTURE ──
            origin_hit = next((p for p in trip if p["stop_order"] == lo), None)

            # LOOP DWELL: on a loop route the origin stop IS the terminus stop,
            # so the passage recorded at `lo` is the TERMINAL EVENT — the bus
            # arriving from the previous lap. If it then sat there (measured on
            # 619: order 1 at 07:00, order 3 at 07:27 — 27 minutes later), that
            # passage is NOT the departure: taking it as one folded the whole
            # dwell into the trip duration (88′ instead of ~45′). When later
            # origin-side passages exist beyond LOOP_DWELL_MINS, the departure
            # is derived from THOSE and the `lo` passage is dropped from the
            # origin-side fit. Short gaps (< LOOP_DWELL_MINS) mean the bus left
            # right away, so nothing changes there.
            dwell_applied = False
            terminal_event_dt = None
            if loop_mid is not None and origin_hit and origin_side:
                oh_dt = _parse(origin_hit["passed_at"])
                later = [(o, t) for o, t in origin_side
                         if t > oh_dt + timedelta(minutes=LOOP_DWELL_MINS)]
                if later:
                    origin_side = later
                    origin_hit = None          # it was an arrival, not a start
                    dwell_applied = True
                    terminal_event_dt = oh_dt

            if origin_hit:
                started_dt = _parse(origin_hit["passed_at"])
            elif (origin_side and segments.get(min(o for o, _t in origin_side))
                  and not (len(origin_side) >= 2
                           and max(o for o, _t in origin_side)
                               - min(o for o, _t in origin_side) >= LINFIT_MIN_SPAN)):
                # Learned segment: the earliest observed origin-side stop has a
                # measured origin→stop median, so subtract exactly that. Used in
                # preference to the linear fit UNLESS the observed points span
                # several stops — with two adjacent stops (≈1 min apart) the fit
                # derives its slope from a 60-second base with ±30 s noise per
                # point, i.e. ~±100% slope error, which extrapolation multiplies.
                # A 30-day median is far steadier. With a wide span the fit is
                # well conditioned AND reflects today's traffic, so it wins.
                o = min(oo for oo, _t in origin_side)
                t = min(tt for oo, tt in origin_side if oo == o)
                started_dt = t - timedelta(minutes=segments[o])
                earliest = min(tt for _o, tt in origin_side)
                if started_dt > earliest:
                    started_dt = earliest
            elif len(origin_side) >= 2:
                # Guard: out-of-order edge passages (3 seen before 2) give the
                # fit a negative slope, projecting the departure AFTER the
                # vehicle was already observed. A departure can never postdate
                # its own first passage — fall back to that observation.
                earliest = min(t for _o, t in origin_side)
                pred = _linfit_predict(origin_side, lo, distances)
                started_dt = pred if (pred and pred <= earliest) else earliest
            elif origin_side:
                # No learned segment yet: step back with the route's uniform
                # per-stop pace (less accurate, hence the segment path above).
                o, t = origin_side[0]
                if route_duration and o > lo:
                    if distances and distances.get(hi):
                        # Distance-weighted: the share of the route actually
                        # covered, not the share of stop indices.
                        frac = (distances.get(o, 0) - distances.get(lo, 0)) / distances[hi]
                        started_dt = t - timedelta(minutes=route_duration * max(0.0, frac))
                    else:
                        pace = route_duration * 60.0 / max(1, hi - lo)
                        started_dt = t - timedelta(seconds=(o - lo) * pace)
                else:
                    started_dt = t
            elif term_side and route_duration:
                started_dt = term_side[-1][1] - timedelta(minutes=route_duration)
            else:
                started_dt = first_dt

            # ── ARRIVAL ──
            term_hit = next((p for p in trip if p["stop_order"] == hi), None)
            if term_hit:
                terminus_dt = _parse(term_hit["passed_at"])
            elif len(term_side) >= 2:
                terminus_dt = _linfit_predict(term_side, hi, distances)
            elif len(term_side) == 1 and route_duration:
                # Single near-terminus passage (e.g. stop 35/36): extend by the
                # route's typical per-stop pace for the 1-3 remaining stops.
                # Tightly bounded (≤3 stops ≈ ≤ a few minutes), so the estimate
                # stays close to reality — this also rescues circular routes,
                # whose final stop doubles as the next trip's origin.
                order, tdt = term_side[0]
                remaining = hi - order
                if 0 < remaining <= 3:
                    seg = segments.get(order)
                    if seg is not None and route_duration - seg >= 0:
                        # Learned: time from origin to this stop is measured, so
                        # what remains to the terminus is duration − segment.
                        # Uniform pace over-estimates the tail, because terminal
                        # approaches are faster than the route average.
                        terminus_dt = tdt + timedelta(minutes=route_duration - seg)
                    elif distances and distances.get(hi):
                        frac = (distances[hi] - distances.get(order, 0)) / distances[hi]
                        terminus_dt = tdt + timedelta(minutes=route_duration * max(0.0, frac))
                    else:
                        pace_secs = route_duration * 60.0 / max(1, hi - lo)
                        terminus_dt = tdt + timedelta(seconds=remaining * pace_secs)
                else:
                    terminus_dt = None
            else:
                terminus_dt = None   # incomplete — never observed finishing

            # Cap on ESTIMATES only: an extrapolated arrival that runs past the
            # vehicle's next observed passage produced the "arrives 07:13 but
            # departs 07:11" inversions. The cap never pulls the arrival before
            # this lap's own last real passage — on loop routes the next lap's
            # origin stop is physically passed BEFORE the arrival stops, so the
            # raw next-passage time can legitimately precede the arrival.
            if terminus_dt and term_hit is None and next_first_dt:
                hard_cap = max(next_first_dt, last_dt)
                if terminus_dt > hard_cap:
                    terminus_dt = hard_cap

            # PLAUSIBILITY of the dwell-derived departure: measured on 619,
            # order 1 @ 11:14 then order 3 @ 12:07 then order 40 @ 12:32 — a
            # ~50′ route cannot be covered in 25′, so those 12:07 points are not
            # a departure (OASA briefly predicts the vehicle's NEXT trip while
            # it is still parked). When the dwell rule yields a duration below
            # LOOP_MIN_DURATION_FRACTION of the route's own median, fall back to
            # "arrival − typical duration", never earlier than the terminal
            # event that closed the previous lap.
            if (dwell_applied and terminus_dt and started_dt and route_duration):
                dur_mins = (terminus_dt - started_dt).total_seconds() / 60
                if dur_mins < route_duration * LOOP_MIN_DURATION_FRACTION:
                    est = terminus_dt - timedelta(minutes=route_duration)
                    if terminal_event_dt and est < terminal_event_dt:
                        est = terminal_event_dt
                    started_dt = est

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
            zone = timedelta(minutes=_boundary_zone_mins(conn, route_code,
                                                         service_date))
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
