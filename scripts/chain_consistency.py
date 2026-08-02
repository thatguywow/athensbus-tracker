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



# Πάνω από αυτό, η επικάλυψη δεν είναι αλλαγή κατεύθυνσης στο τερματικό αλλά
# πραγματική ανωμαλία — και πρέπει να ΜΕΙΝΕΙ σημαδεμένη, όχι να συμμαζευτεί.
HANDOVER_MAX_OVERLAP_MINS = 15.0


def _resolve_handover_overlaps(conn, service_date: str) -> tuple[int, set]:
    """
    Ένα όχημα δεν αναχωρεί πριν φτάσει.

    ΜΕΤΡΗΜΕΝΟ στην πλήρη ημέρα 2026-08-01: 515 επικαλύψεις όπου το ΙΔΙΟ όχημα
    «τρέχει» δύο διαδρομές ΤΗΣ ΙΔΙΑΣ ΓΡΑΜΜΗΣ ταυτόχρονα. Οι 501 από αυτές είναι
    εναλλαγή κατεύθυνσης (εξερχόμενη→εισερχόμενη ή αντίστροφα), και οι δύο με
    πυκνά δεδομένα (31-33 σημεία η καθεμιά). Διάμεση επικάλυψη: 1,4 λεπτά.

    Δεν είναι σφάλμα μέτρησης — είναι ΓΕΩΓΡΑΦΙΑ. Το τέρμα της μιας κατεύθυνσης
    και η αφετηρία της άλλης είναι το ΙΔΙΟ φυσικό σημείο, οπότε το στίγμα του
    οχήματος προβάλλεται και στις δύο διαδρομές και οι δύο διελεύσεις
    μπλέκονται χρονικά.

    Η λύση δεν είναι να διαλέξουμε νικητή: και οι δύο μετρήσεις είναι σωστές
    ξεχωριστά. Είναι να τοποθετήσουμε την ΑΛΛΑΓΗ στο μέσο της αμφισβητούμενης
    ζώνης — η άφιξη της πρώτης και η αναχώρηση της δεύτερης γίνονται η ίδια
    στιγμή, που είναι ούτως ή άλλως η φυσική αλήθεια σε ένα τερματικό.

    Μεγάλες επικαλύψεις ΔΕΝ αγγίζονται: εκεί κάτι άλλο συμβαίνει και ο έλεγχος
    ποιότητας πρέπει να συνεχίσει να το βλέπει.
    """
    from datetime import datetime

    rows = conn.execute("""
        SELECT id, route_code, vehicle_no, started_at, terminus_arrived_at
        FROM trips WHERE service_date=? AND terminus_arrived_at IS NOT NULL
        ORDER BY vehicle_no, started_at""", (service_date,)).fetchall()

    by_veh: dict[str, list] = {}
    for r in rows:
        by_veh.setdefault(r["vehicle_no"], []).append(dict(r))

    fixed, affected = 0, set()
    for trips in by_veh.values():
        for a, b in zip(trips, trips[1:]):
            arr, dep = a["terminus_arrived_at"], b["started_at"]
            if not arr or not dep or arr <= dep:
                continue
            try:
                ta, tb = datetime.fromisoformat(arr), datetime.fromisoformat(dep)
            except (ValueError, TypeError):
                continue
            overlap = (ta - tb).total_seconds() / 60.0
            if overlap <= 0 or overlap > HANDOVER_MAX_OVERLAP_MINS:
                continue
            # ΜΟΝΟ η αναχώρηση της ΕΠΟΜΕΝΗΣ μετακινείται, όχι η άφιξη της
            # προηγούμενης. Η άφιξη κλείνει μια διαδρομή που παρατηρήθηκε από
            # την αρχή ως το τέλος — είναι η πιο σίγουρη τιμή που έχουμε. Η
            # αναχώρηση της επόμενης είναι η αμφίσημη: το όχημα στέκεται ακόμη
            # στο τερματικό και το στίγμα του ταιριάζει και στις δύο διαδρομές.
            # (Δοκιμάστηκε και η λύση «μέσο σημείο»: έλυνε εξίσου τις
            # επικαλύψεις αλλά κόντυνε ΚΑΙ ΤΑ ΔΥΟ δρομολόγια, εκτοξεύοντας το
            # duration_too_short από 36 σε 289.)
            conn.execute("UPDATE trips SET started_at=? WHERE id=?",
                         (arr, b["id"]))
            conn.execute("UPDATE vehicle_departures SET departed_at=? "
                         "WHERE trip_id=?", (arr, b["id"]))
            b["started_at"] = arr
            fixed += 1
            affected.add(a["route_code"])
            affected.add(b["route_code"])
    if fixed:
        log.info("Chain consistency: %d αλλαγές κατεύθυνσης στο τερματικό "
                 "ευθυγραμμίστηκαν (%d διαδρομές)", fixed, len(affected))
    return fixed, affected



# Ελάχιστο κλάσμα της τυπικής διάρκειας κάτω από το οποίο μια διάρκεια δεν
# είναι «γρήγορο δρομολόγιο» αλλά αδύνατη. Ίδια τιμή με το
# MIN_DURATION_FRACTION της ανακατασκευής — είναι ο ΙΔΙΟΣ κανόνας, που εδώ
# απλώς ξαναεπιβάλλεται.
MIN_DURATION_FRACTION = 0.3


def _reassert_duration_sanity(conn, service_date: str) -> tuple[int, set]:
    """
    Ξαναεπιβάλλει τον κανόνα πιθανοφάνειας που έσπασε το ίδιο το tighten_chain.

    Η ανακατασκευή ελέγχει ήδη ότι μια άφιξη δεν υπονοεί διάρκεια μικρότερη από
    MIN_DURATION_FRACTION της τυπικής. Το σφίξιμο όμως τρέχει ΜΕΤΑ και μπορεί να
    μετακινήσει την ΕΚΤΙΜΩΜΕΝΗ αναχώρηση πολύ μπροστά — οπότε ο έλεγχος που
    πέρασε τη στιγμή της κατασκευής παύει να ισχύει.

    ΜΕΤΡΗΜΕΝΟ (2026-08-01): 289 δρομολόγια με «αδύνατα μικρή διάρκεια», εκ των
    οποίων το 95% καλύπτει λιγότερο από το 60% της διαδρομής και η διάμεση
    κάλυψη είναι 9%. Χαρακτηριστικό δείγμα: ΜΙΑ διέλευση στη στάση 100 από 100,
    διάρκεια 1,1′ έναντι τυπικής 89′. Δεν είναι δρομολόγιο — είναι η ουρά ενός
    γύρου που δεν είδαμε από την αρχή, συνήθως επειδή ο ΟΑΣΑ μετέθεσε το όχημα
    σε αυτή τη διαδρομή τη στιγμή που τελείωνε.

    Η τιμή που ΞΕΡΟΥΜΕ είναι η άφιξη· αυτή που δεν ξέρουμε είναι η αναχώρηση.
    Το να δηλώνουμε διάρκεια 1 λεπτού είναι χειρότερο από το να μη δηλώνουμε
    καμία. Οπότε: η άφιξη μηδενίζεται (το δρομολόγιο μένει «ημιτελές»), και η
    μέτρηση δεν μολύνει καμία στατιστική διάρκειας.

    ΔΕΝ αγγίζονται δρομολόγια με ΜΕΤΡΗΜΕΝΗ αναχώρηση: εκεί και οι δύο άκρες
    είναι παρατηρημένες και μια σύντομη διάρκεια είναι πραγματικό γεγονός.
    """
    rows = conn.execute("""
        SELECT t.id, t.route_code,
               (julianday(t.terminus_arrived_at) - julianday(t.started_at)) * 1440 dur,
               rr.median_trip_duration_mins med,
               EXISTS (SELECT 1 FROM trip_stop_times x
                        WHERE x.trip_id = t.id AND x.method='passage'
                          AND x.stop_order = (SELECT MIN(stop_order) FROM stops s
                                              WHERE s.route_code = t.route_code)) dep_measured
        FROM trips t
        JOIN route_rotation rr ON rr.route_code = t.route_code
        WHERE t.service_date = ? AND t.terminus_arrived_at IS NOT NULL
          AND rr.median_trip_duration_mins IS NOT NULL
          AND rr.median_trip_duration_mins > 0""", (service_date,)).fetchall()

    fixed, affected = 0, set()
    for r in rows:
        if r["dep_measured"] or r["dur"] is None:
            continue
        if r["dur"] >= MIN_DURATION_FRACTION * r["med"]:
            continue
        conn.execute("UPDATE trips SET terminus_arrived_at=NULL WHERE id=?", (r["id"],))
        fixed += 1
        affected.add(r["route_code"])
    if fixed:
        log.info("Chain consistency: %d αδύνατα σύντομες διάρκειες σημάνθηκαν "
                 "ημιτελείς (%d διαδρομές)", fixed, len(affected))
    return fixed, affected


def tighten_chain(conn, service_date: str, computed_at: str) -> dict:
    dropped_routes = _drop_contained_fragments(conn, service_date)
    n_handover, handover_routes = _resolve_handover_overlaps(conn, service_date)

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
    n_sane, sane_routes = _reassert_duration_sanity(conn, service_date)
    affected |= sane_routes

    return {"routes": affected | dropped_routes | handover_routes,
            "departures": n_dep, "arrivals": n_arr,
            "dropped": len(dropped_routes), "handovers": n_handover}
