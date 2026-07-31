"""
validate_interpolation.py — πόσο λάθος είναι η υπόθεση σταθερής ταχύτητας;

ΤΟ ΕΡΩΤΗΜΑ
==========
Η ώρα διέλευσης από μια στάση προκύπτει με ΓΡΑΜΜΙΚΗ ΠΑΡΕΜΒΟΛΗ ανάμεσα σε δύο
στίγματα GPS. Τα άλλα σφάλματα είναι μικρά και γνωστά (ακρίβεια GPS ~5 m ≈ 1 s,
snap στάσης 7-12 m ≈ 2 s, μετατόπιση άκρων 25 m ≈ 4-5 s σταθερά). Αυτό εδώ
είναι το ΜΟΝΟ που δεν ξέρουμε: ένα λεωφορείο που πιάνει κόκκινο ανάμεσα σε δύο
στίγματα ΔΕΝ κινείται με σταθερή ταχύτητα.

Η ΜΕΘΟΔΟΣ (hold-out)
====================
Παίρνουμε τρία διαδοχικά στίγματα A → B → C του ίδιου οχήματος.
ΚΡΥΒΟΥΜΕ το B. Προβλέπουμε πού ήταν το όχημα τη στιγμή tB, παρεμβάλλοντας
ανάμεσα σε A και C — δηλαδή με ΑΚΡΙΒΩΣ τον μηχανισμό που χρησιμοποιούν οι ώρες
διέλευσης. Μετά συγκρίνουμε με το πού ΟΝΤΩΣ ήταν, σύμφωνα με τον ΟΑΣΑ.

    A ─────────── B ─────────── C        πραγματικό
    A ─────────── ? ─────────── C        πρόβλεψη
                  ↑
            σφάλμα σε μέτρα → σε δευτερόλεπτα (διαιρώντας με την ταχύτητα)

ΣΗΜΑΝΤΙΚΟ — ΑΝΩ ΦΡΑΓΜΑ, ΟΧΙ ΑΚΡΙΒΗΣ ΤΙΜΗ
=========================================
Εδώ παρεμβάλλουμε πάνω από ΔΥΟ διαστήματα (A→C ≈ 70 s). Στην πραγματική χρήση
η παρεμβολή γίνεται μέσα σε ΕΝΑ διάστημα (~35 s). Το σφάλμα μεγαλώνει με το
κενό, οπότε το πραγματικό σφάλμα είναι ΜΙΚΡΟΤΕΡΟ από αυτό που μετράει εδώ —
κατά προσέγγιση το μισό. Το κρατάμε συντηρητικό επίτηδες.

    python scripts/validate_interpolation.py
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import geo

MIN_SPEED_MS = 1.0      # κάτω από 3,6 km/h το όχημα ουσιαστικά στέκεται:
                        # η μετατροπή μέτρων σε δευτερόλεπτα εκρήγνυται
MAX_GAP_S = 180.0       # πολύ μεγάλα κενά δεν αντιπροσωπεύουν κανονική λειτουργία
MIN_SPAN_M = 30.0       # χρειάζεται κάποια κίνηση για να έχει νόημα


def load_shapes(conn, route_codes):
    out = {}
    for rc in route_codes:
        pts = conn.execute("SELECT lat, lng, dist_m FROM route_shapes "
                           "WHERE route_code=? ORDER BY seq", (rc,)).fetchall()
        if len(pts) >= 2:
            out[rc] = geo.RouteShape([(p["lat"], p["lng"]) for p in pts],
                                     dists=[p["dist_m"] for p in pts])
    return out


def main():
    conn = db.get_connection()
    route_codes = [r["route_code"] for r in
                   conn.execute("SELECT DISTINCT route_code FROM vehicle_pings")]
    if not route_codes:
        print("Δεν υπάρχουν στίγματα. Τρέξε: gps_tracker.py --store-pings")
        return
    shapes = load_shapes(conn, route_codes)
    print(f"Διαδρομές με στίγματα: {len(route_codes)}  (με γεωμετρία: {len(shapes)})")

    rows = conn.execute("""
        SELECT route_code, vehicle_no, lat, lng, ts_utc
        FROM vehicle_pings ORDER BY route_code, vehicle_no, ts_utc""").fetchall()

    tracks = defaultdict(list)
    for r in rows:
        tracks[(r["route_code"], r["vehicle_no"])].append(
            (datetime.fromisoformat(r["ts_utc"]), r["lat"], r["lng"]))

    err_m, err_s, gaps, speeds = [], [], [], []
    by_gap = defaultdict(list)
    triples = skipped = 0

    for (rc, veh), pts in tracks.items():
        shape = shapes.get(rc)
        if shape is None or len(pts) < 3:
            continue
        # Προβολή με ΤΗ ΣΕΙΡΑ, κουβαλώντας τον δρομέα: ίδια λογική με τον
        # tracker (χωρίς αυτό, κυκλικές διαδρομές δίνουν λάθος θέση).
        prog, cursor = [], None
        for t, lat, lng in pts:
            p = shape.project(lat, lng, near_dist=cursor)
            if p is None:
                prog.append(None)
                continue
            prog.append((t, p[0]))
            cursor = p[0]

        for i in range(1, len(prog) - 1):
            a, b, c = prog[i - 1], prog[i], prog[i + 1]
            if not (a and b and c):
                continue
            (ta, da), (tb, dbb), (tc, dc) = a, b, c
            gap = (tc - ta).total_seconds()
            span = dc - da
            if gap <= 0 or gap > MAX_GAP_S or span < MIN_SPAN_M:
                skipped += 1
                continue
            speed = span / gap                      # m/s μέσος όρος A→C
            if speed < MIN_SPEED_MS:
                skipped += 1
                continue
            pred = da + span * ((tb - ta).total_seconds() / gap)
            e_m = abs(pred - dbb)
            err_m.append(e_m)
            err_s.append(e_m / speed)
            gaps.append(gap)
            speeds.append(speed * 3.6)
            by_gap[int(gap // 20) * 20].append(e_m / speed)
            triples += 1

    if not err_m:
        print("Δεν βρέθηκαν αξιοποιήσιμες τριάδες.")
        return

    def q(v, p):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * p))]

    print(f"\nΤριάδες που αξιολογήθηκαν: {triples:,}  (παραλείφθηκαν {skipped:,})")
    print(f"Διάμεσο κενό A→C: {statistics.median(gaps):.0f}s   "
          f"διάμεση ταχύτητα: {statistics.median(speeds):.1f} km/h")

    print("\n" + "=" * 62)
    print("ΣΦΑΛΜΑ ΠΑΡΕΜΒΟΛΗΣ  (πάνω από ΔΥΟ διαστήματα — άνω φράγμα)")
    print("=" * 62)
    print(f"{'':12}{'διάμεσος':>10}{'p75':>9}{'p90':>9}{'p95':>9}{'max':>10}")
    print(f"{'μέτρα':<12}{statistics.median(err_m):>10.1f}{q(err_m,.75):>9.1f}"
          f"{q(err_m,.90):>9.1f}{q(err_m,.95):>9.1f}{max(err_m):>10.1f}")
    print(f"{'δευτ/πτα':<12}{statistics.median(err_s):>10.1f}{q(err_s,.75):>9.1f}"
          f"{q(err_s,.90):>9.1f}{q(err_s,.95):>9.1f}{max(err_s):>10.1f}")

    print("\nΣφάλμα (δευτ.) ανά μήκος κενού — δείχνει πώς κλιμακώνεται:")
    for g in sorted(by_gap):
        v = by_gap[g]
        if len(v) >= 20:
            print(f"  κενό {g:>3}-{g+19:>3}s : n={len(v):>5}  "
                  f"διάμεσος={statistics.median(v):>5.1f}s  p90={q(v,.90):>5.1f}s")

    half = statistics.median(err_s) / 2
    print("\n" + "=" * 62)
    print(f"ΕΚΤΙΜΩΜΕΝΟ ΠΡΑΓΜΑΤΙΚΟ ΣΦΑΛΜΑ (ένα διάστημα ~35s): ~{half:.1f}s διάμεσος")
    print("Για σύγκριση, η ανίχνευση εξαφάνισης: ~20s ΣΥΣΤΗΜΑΤΙΚΗ μεροληψία")
    print("συν ±30-60s τυχαίο (btime2 σε ακέραια λεπτά + πλάτος κύκλου).")
    print("=" * 62)
    conn.close()


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    main()
