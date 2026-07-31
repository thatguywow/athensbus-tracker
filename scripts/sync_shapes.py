"""
sync_shapes.py — κατεβάζει τη ΓΕΩΜΕΤΡΙΑ κάθε διαδρομής και τοποθετεί τις
στάσεις πάνω της.

Τρέχει μαζί με το sync_master_data (εβδομαδιαία). Γεμίζει δύο πίνακες:

  route_shapes         η πραγματική διαδρομμένη πολυγραμμή, με σωρευτικά μέτρα
  stop_shape_offsets   σε πόσα μέτρα της πολυγραμμής βρίσκεται κάθε στάση

Μαζί, αυτά μετατρέπουν κάθε στίγμα GPS σε έναν αριθμό («μέτρα διανυθέντα») και
κάθε στάση σε έναν αριθμό στον ίδιο άξονα. Η ώρα διέλευσης γίνεται τότε
γραμμική παρεμβολή, όχι εικασία εγγύτητας.

    python scripts/sync_shapes.py                # όλες οι διαδρομές
    python scripts/sync_shapes.py 2051 5346      # συγκεκριμένες
"""

from __future__ import annotations

import logging
import sys
import time

import db
import geo
import oasa_client as oasa

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_shapes")

# Ρυθμός κλήσεων. Το probe δείχνει καθαρό (<1% 403) μέχρι ~20/s· εδώ είμαστε
# συντηρητικοί γιατί η δουλειά είναι εβδομαδιαία και δεν βιάζεται καθόλου —
# 715 διαδρομές στα 8/s = ~90 δευτερόλεπτα, μία φορά την εβδομάδα.
PACE_RATE = 8.0

# Πάνω από αυτό, η στάση δεν κάθεται στην πολυγραμμή που κατεβάσαμε: είτε το
# σχήμα είναι εκτροπής ενώ η λίστα στάσεων είναι η κανονική, είτε τα δεδομένα
# διαφωνούν. Καταγράφουμε το γεγονός· η παραγωγή διελεύσεων από GPS αγνοεί
# αυτές τις διαδρομές αντί να βγάλει σίγουρες ανοησίες.
SUSPECT_SNAP_M = 150.0


def fetch_shape(route_code: str) -> list[tuple[float, float]]:
    """Πολυγραμμή ως [(lat, lng), ...] με τη σειρά διαδρομής."""
    data = oasa.web_get_routes_details_and_stops(route_code)
    details = data.get("details") or []
    pts = []
    for d in details:
        try:
            # ΠΡΟΣΟΧΗ: routed_x = ΜΗΚΟΣ (23.x), routed_y = ΠΛΑΤΟΣ (37.x).
            # Ανάποδα από τη διαίσθηση «x=lat», και σιωπηλά καταστροφικό.
            lng = float(d["routed_x"])
            lat = float(d["routed_y"])
            order = int(d["routed_order"])
        except (KeyError, TypeError, ValueError):
            continue
        pts.append((order, lat, lng))
    pts.sort(key=lambda p: p[0])        # αριθμητικά, όχι αλφαβητικά
    return [(lat, lng) for _o, lat, lng in pts]


def store_route(conn, route_code: str, pts: list[tuple[float, float]],
                synced_at: str) -> dict:
    if len(pts) < 2:
        return {"route_code": route_code, "points": 0, "stops": 0,
                "status": "no_geometry"}

    shape = geo.RouteShape(pts)

    conn.execute("DELETE FROM route_shapes WHERE route_code=?", (route_code,))
    conn.executemany(
        "INSERT INTO route_shapes (route_code, seq, lat, lng, dist_m, last_synced)"
        " VALUES (?,?,?,?,?,?)",
        [(route_code, i, shape.lat[i], shape.lng[i], shape.dist[i], synced_at)
         for i in range(shape.n)])

    stops = [(r["stop_order"], r["stop_code"], r["lat"], r["lng"])
             for r in conn.execute(
                 "SELECT stop_order, stop_code, lat, lng FROM stops "
                 "WHERE route_code=? AND lat IS NOT NULL AND lng IS NOT NULL "
                 "ORDER BY stop_order", (route_code,))]
    if not stops:
        return {"route_code": route_code, "points": shape.n, "stops": 0,
                "status": "no_stops"}

    snapped = geo.snap_stops(shape, stops)
    conn.execute("DELETE FROM stop_shape_offsets WHERE route_code=?", (route_code,))
    conn.executemany(
        "INSERT INTO stop_shape_offsets "
        "(route_code, stop_order, stop_code, dist_m, snap_err_m, last_synced)"
        " VALUES (?,?,?,?,?,?)",
        [(route_code, o, c, d, (None if err == float("inf") else round(err, 1)),
          synced_at) for o, c, d, err in snapped])

    errs = [e for _o, _c, _d, e in snapped if e != float("inf")]
    worst = max(errs) if errs else float("inf")
    return {
        "route_code": route_code,
        "points": shape.n,
        "stops": len(snapped),
        "length_km": round(shape.total / 1000.0, 2),
        "worst_snap_m": round(worst, 1) if worst != float("inf") else None,
        "status": "ok" if worst <= SUSPECT_SNAP_M else "suspect_snap",
    }


def sync_all(conn, route_codes: list[str], synced_at: str,
             limiter=None) -> dict:
    """
    Κατεβάζει τη γεωμετρία για τις δοσμένες διαδρομές. Καλείται και από το
    sync_master_data, ώστε το σχήμα να ανανεώνεται μαζί με στάσεις/διαδρομές —
    ο ΟΑΣΑ αλλάζει πορείες (π.χ. «temporary detour due to roadworks») και μια
    παλιά πολυγραμμή βγάζει σιωπηλά λάθος αποστάσεις.
    """
    limiter = limiter or oasa._SimpleLimiter(PACE_RATE)
    ok = suspect = failed = 0
    t0 = time.time()
    for i, rc in enumerate(route_codes, 1):
        limiter.acquire()
        try:
            res = store_route(conn, rc, fetch_shape(rc), synced_at)
        except Exception as e:
            log.warning("  %s: %s", rc, e)
            failed += 1
            continue
        if res["status"] == "ok":
            ok += 1
        elif res["status"] == "suspect_snap":
            suspect += 1
            log.warning("  %s: στάσεις έως %.0f m από την πολυγραμμή",
                        rc, res["worst_snap_m"])
        else:
            failed += 1
        if i % 100 == 0:
            conn.commit()
            log.info("  %d/%d (%.0fs)", i, len(route_codes), time.time() - t0)
    conn.commit()
    log.info("Γεωμετρία: %d εντάξει, %d ύποπτο snap, %d απέτυχαν (%.0fs)",
             ok, suspect, failed, time.time() - t0)
    return {"ok": ok, "suspect": suspect, "failed": failed}


def main():
    db.ensure_schema()
    synced_at = db.now_utc_iso()
    conn = db.get_connection()

    try:
        if len(sys.argv) > 1:
            route_codes = sys.argv[1:]
        else:
            route_codes = [r["route_code"] for r in
                           conn.execute("SELECT route_code FROM routes "
                                        "ORDER BY route_code")]
        if not route_codes:
            log.error("Καμία διαδρομή στη βάση — τρέξε πρώτα sync_master_data.")
            sys.exit(1)

        log.info("Γεωμετρία για %d διαδρομές στα %.0f req/s…",
                 len(route_codes), PACE_RATE)
        sync_all(conn, route_codes, synced_at)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
