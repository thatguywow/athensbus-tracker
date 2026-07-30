"""
chain_consistency.py — «καμία εκτίμηση δεν αντιφάσκει με μέτρηση».

Η ανακατασκευή δουλεύει ΑΝΑ ΔΙΑΔΡΟΜΗ, οπότε δεν μπορεί να δει ότι το ίδιο
όχημα ήταν, μετρημένα, αλλού. Παράδειγμα από πραγματικά δεδομένα (όχημα 61210):

    02:02  ΜΕΤΡΗΜΕΝΗ αναχώρηση, διαδρομή 2138 (ΑΚΑΔΗΜΙΑ→ΑΝΘΟΥΣΑ)
    02:34  ΜΕΤΡΗΜΕΝΗ άφιξη ΑΝΘΟΥΣΑ
    03:19  ΜΕΤΡΗΜΕΝΗ άφιξη ΑΚΑΔΗΜΙΑ (επιστροφή, διαδρομή 3609)

Η αναχώρηση της επιστροφής δεν παρατηρήθηκε και εκτιμήθηκε ως 03:19 − 52′ =
02:27 — τη στιγμή που το όχημα ήταν, μετρημένα, καθ' οδόν προς ΑΝΘΟΥΣΑ. Ψεύτικη
επικάλυψη, και σφάλμα 13′.

Αυτό το πέρασμα χτίζει τη χρονογραμμή κάθε οχήματος από ΟΛΕΣ τις διελεύσεις
του (όλες οι διαδρομές) και σφίγγει ΜΟΝΟ τις εκτιμήσεις στα όρια που ορίζουν οι
μετρήσεις:

    εκτιμώμενη αναχώρηση ≥ τελευταία διέλευση πριν από αυτήν, και ≤ πρώτη
                            διέλευση του ίδιου δρομολογίου
    εκτιμώμενη άφιξη     ≤ επόμενη διέλευση μετά από αυτήν, και ≥ τελευταία
                            διέλευση του ίδιου δρομολογίου

Καμία μετρημένη τιμή δεν αγγίζεται, τίποτα δεν διαγράφεται, και το πέρασμα
είναι idempotent (δεύτερη εκτέλεση δεν αλλάζει τίποτα). Τρέχει σε όλη τη μέρα
και επιστρέφει ΠΟΙΕΣ διαδρομές άλλαξε, ώστε ο επιλεκτικός υπολογισμός να
συμπεριλάβει και αυτές στα slots/στατιστικά.
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right

log = logging.getLogger(__name__)


def _drop_contained_fragments(conn, service_date: str) -> set[str]:
    """
    Remove re-detection fragments: a 1-2 point trip with no measured arrival
    whose whole observed span sits INSIDE another trip of the SAME vehicle.

    Measured on 619: while vehicle 71284 was running 17:05→17:58, a lone
    passage (order 3 @ 17:35) reappeared in the predictions and opened a
    second "trip". A bus cannot run two laps at once, so the fragment is a
    duplicate detection of the lap already in progress — it stole a schedule
    slot and broke the alternation between the line's two vehicles.

    Deliberately strict: complete trips, trips with a measured arrival, and
    trips that merely overlap at the edges are never touched.
    """
    rows = [dict(r) for r in conn.execute("""
        SELECT t.id, t.route_code, t.vehicle_no, t.terminus_arrived_at,
               (SELECT COUNT(*) FROM trip_stop_times x
                 WHERE x.trip_id = t.id AND x.method='passage') n_pts,
               (SELECT MIN(x.passed_at) FROM trip_stop_times x
                 WHERE x.trip_id = t.id AND x.method='passage') first_obs,
               (SELECT MAX(x.passed_at) FROM trip_stop_times x
                 WHERE x.trip_id = t.id AND x.method='passage') last_obs
        FROM trips t WHERE t.service_date = ?
    """, (service_date,))]

    by_vehicle: dict[str, list[dict]] = {}
    for r in rows:
        by_vehicle.setdefault(r["vehicle_no"], []).append(r)

    doomed: list[dict] = []
    for trips in by_vehicle.values():
        for t in trips:
            # Only thin fragments qualify: a real lap leaves more evidence.
            if t["n_pts"] > 2 or not t["first_obs"]:
                continue
            for other in trips:
                if other["id"] == t["id"] or not other["first_obs"]:
                    continue
                # STRICT overlap: two laps of one bus cannot run at the same
                # time. Touching at a single instant (lap A's arrival is lap
                # B's departure — normal on loop routes) is NOT an overlap.
                overlaps = (t["first_obs"] < other["last_obs"]
                            and t["last_obs"] > other["first_obs"])
                if overlaps and other["n_pts"] > t["n_pts"]:
                    doomed.append(t)
                    break

    affected: set[str] = set()
    for t in doomed:
        conn.execute("DELETE FROM slot_assignments WHERE trip_id=?", (t["id"],))
        conn.execute("UPDATE vehicle_departures SET trip_id=NULL WHERE trip_id=?", (t["id"],))
        conn.execute("DELETE FROM trip_stop_times WHERE trip_id=?", (t["id"],))
        conn.execute("DELETE FROM trips WHERE id=?", (t["id"],))
        affected.add(t["route_code"])
    if doomed:
        log.info("Chain consistency: %d θραύσματα επανα-ανίχνευσης αφαιρέθηκαν "
                 "(%d διαδρομές)", len(doomed), len(affected))
    return affected


def tighten_chain(conn, service_date: str, computed_at: str) -> dict:
    dropped_routes = _drop_contained_fragments(conn, service_date)

    # Every observed passage of every vehicle on this service day.
    # #9 CROSS-DAY: a vehicle finishing at 03:50 and starting again at 04:10
    # crosses the service-day boundary, so a day-limited timeline cannot see
    # the conflict. Observations from the adjacent days are included when
    # building each vehicle's timeline — they only ever TIGHTEN estimates,
    # never create or move trips of another day.
    from datetime import date as _date, timedelta as _td
    d0 = _date.fromisoformat(service_date)
    days = [(d0 - _td(days=1)).isoformat(), service_date,
            (d0 + _td(days=1)).isoformat()]
    obs: dict[str, list[str]] = {}
    for r in conn.execute("""
            SELECT t.vehicle_no vn, x.passed_at pa
            FROM trip_stop_times x
            JOIN trips t ON t.id = x.trip_id
            WHERE t.service_date IN (?,?,?) AND x.method = 'passage'
            ORDER BY t.vehicle_no, x.passed_at""", days):
        obs.setdefault(r["vn"], []).append(r["pa"])

    trips = conn.execute("""
        SELECT t.id, t.route_code, t.vehicle_no, t.started_at, t.terminus_arrived_at,
               (SELECT MIN(x.passed_at) FROM trip_stop_times x
                 WHERE x.trip_id = t.id AND x.method='passage') first_obs,
               (SELECT MAX(x.passed_at) FROM trip_stop_times x
                 WHERE x.trip_id = t.id AND x.method='passage') last_obs,
               EXISTS (SELECT 1 FROM trip_stop_times x
                        WHERE x.trip_id = t.id AND x.method='passage'
                          AND x.stop_order = (SELECT MIN(stop_order) FROM stops s
                                              WHERE s.route_code = t.route_code)) dep_measured,
               EXISTS (SELECT 1 FROM trip_stop_times x
                        WHERE x.trip_id = t.id AND x.method='passage'
                          AND x.stop_order = (SELECT MAX(stop_order) FROM stops s
                                              WHERE s.route_code = t.route_code)) arr_measured
        FROM trips t WHERE t.service_date = ?
    """, (service_date,)).fetchall()

    affected: set[str] = set()
    n_dep = n_arr = 0

    for t in trips:
        times = obs.get(t["vehicle_no"], [])
        dep, arr = t["started_at"], t["terminus_arrived_at"]
        new_dep, new_arr = dep, arr

        # ── estimated DEPARTURE: cannot precede an earlier observation ──
        if dep and not t["dep_measured"] and t["first_obs"]:
            i = bisect_left(times, t["first_obs"])
            prev_obs = times[i - 1] if i > 0 else None
            if prev_obs and new_dep < prev_obs:
                new_dep = prev_obs
            if new_dep > t["first_obs"]:      # never postdate its own first sighting
                new_dep = t["first_obs"]

        # ── estimated ARRIVAL: cannot outlast a later observation ──
        if arr and not t["arr_measured"] and t["last_obs"]:
            j = bisect_right(times, t["last_obs"])
            next_obs = times[j] if j < len(times) else None
            if next_obs and new_arr > next_obs:
                new_arr = next_obs
            if new_arr < t["last_obs"]:       # never predate its own last sighting
                new_arr = t["last_obs"]

        # ── departure can never follow arrival ──
        if new_dep and new_arr and new_dep > new_arr:
            new_dep = min(new_dep, t["first_obs"] or new_arr)

        if new_dep != dep or new_arr != arr:
            conn.execute(
                "UPDATE trips SET started_at=?, terminus_arrived_at=? WHERE id=?",
                (new_dep, new_arr, t["id"]))
            affected.add(t["route_code"])
            n_dep += 1 if new_dep != dep else 0
            n_arr += 1 if new_arr != arr else 0

    if affected:
        log.info("Chain consistency: %d αναχωρήσεις, %d λήξεις σφίχτηκαν "
                 "(%d διαδρομές)", n_dep, n_arr, len(affected))
    return {"routes": affected | dropped_routes,
            "departures": n_dep, "arrivals": n_arr,
            "dropped": len(dropped_routes)}
