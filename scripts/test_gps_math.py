"""
test_gps_math.py — έλεγχος της γεωμετρίας με ΓΝΩΣΤΗ σωστή απάντηση.

Η προβολή και η παρεμβολή είναι σιωπηλά επικίνδυνες: ένα λάθος πρόσημο ή μια
μπερδεμένη σειρά lat/lng δεν σκάει — βγάζει ώρες που μοιάζουν λογικές και είναι
λάθος κατά λεπτά. Εδώ φτιάχνουμε συνθετική διαδρομή όπου η σωστή απάντηση
υπολογίζεται με το χέρι, και απαιτούμε να τη βρει.

    python scripts/test_gps_math.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import geo
from gps_tracker import GpsTracker, RouteGeometry, _parse_cs_date

FAILS = []


def check(name, got, want, tol):
    ok = abs(got - want) <= tol
    print(f"  {'ΟΚ  ' if ok else 'ΛΑΘΟΣ'} {name}: {got:.3f} (αναμ. {want:.3f} ±{tol})")
    if not ok:
        FAILS.append(name)


class FakeGeom(RouteGeometry):
    """RouteGeometry χωρίς βάση, για δοκιμή."""

    def __init__(self, pts, stops):
        self.route_code = "TEST"
        self.shape = geo.RouteShape(pts)
        self.stop_dists = [s[2] for s in stops]
        self.stop_codes = [s[1] for s in stops]
        self.stop_orders = [s[0] for s in stops]
        self.usable = True


def test_scale():
    print("\n1) Συντελεστές μέτρων/μοίρα στην Αθήνα (φ=38°)")
    m_lat, m_lng = geo.latlng_scale(38.0)
    # Αναφορά: WGS84 στο πλάτος 38°
    check("m/μοίρα πλάτους", m_lat, 110996.0, 5.0)
    check("m/μοίρα μήκους", m_lng, 87832.0, 5.0)


def test_straight_line():
    print("\n2) Ευθεία διαδρομή Β→Ν: 1 μοίρα μήκους = γνωστή απόσταση")
    lat0 = 38.0
    pts = [(lat0, 23.7), (lat0, 23.8)]
    shape = geo.RouteShape(pts)
    _m_lat, m_lng = geo.latlng_scale(lat0)
    check("μήκος πολυγραμμής (m)", shape.total, 0.1 * m_lng, 1.0)

    # Σημείο στη μέση, ακριβώς πάνω στη γραμμή
    d, err = shape.project(lat0, 23.75)
    check("προβολή στο μέσο", d, shape.total / 2, 1.0)
    check("σφάλμα κάθετης", err, 0.0, 0.5)

    # Σημείο 50 m βόρεια της γραμμής → ίδια θέση, σφάλμα 50 m
    m_lat, _ = geo.latlng_scale(lat0)
    d2, err2 = shape.project(lat0 + 50.0 / m_lat, 23.75)
    check("προβολή με 50m εκτροπή", d2, shape.total / 2, 1.0)
    check("σφάλμα κάθετης 50m", err2, 50.0, 0.5)


def test_interpolation():
    print("\n3) Παρεμβολή: σταθερή ταχύτητα, γνωστή ώρα διέλευσης")
    # t=0 στο 0 m, t=100 s στα 1000 m → τα 250 m περνιούνται στα 25 s
    check("διέλευση στα 250m", geo.interpolate_crossing(0, 0, 1000, 100, 250),
          25.0, 0.001)
    check("διέλευση στα 999m", geo.interpolate_crossing(0, 0, 1000, 100, 999),
          99.9, 0.001)
    # Εκτός διαστήματος → κόβεται στα άκρα
    check("κόψιμο πάνω", geo.interpolate_crossing(0, 0, 1000, 100, 5000),
          100.0, 0.001)


def test_monotonic_snap():
    print("\n4) Κυκλική διαδρομή: η τελευταία στάση ΔΕΝ γυρίζει στο μηδέν")
    # Τετράγωνο που κλείνει εκεί που άρχισε
    pts = [(38.000, 23.700), (38.010, 23.700), (38.010, 23.710),
           (38.000, 23.710), (38.000, 23.700)]
    shape = geo.RouteShape(pts)
    stops = [(1, "A", 38.000, 23.700),      # αφετηρία
             (2, "B", 38.010, 23.700),
             (3, "C", 38.010, 23.710),
             (4, "D", 38.000, 23.710),
             (5, "A", 38.000, 23.700)]      # ΙΔΙΟ σημείο με την αφετηρία
    snapped = geo.snap_stops(shape, stops)
    dists = [d for _o, _c, d, _e in snapped]
    print(f"     αποστάσεις στάσεων: {[round(d) for d in dists]}")
    mono = all(dists[i] <= dists[i + 1] for i in range(len(dists) - 1))
    print(f"  {'ΟΚ  ' if mono else 'ΛΑΘΟΣ'} μονοτονία")
    if not mono:
        FAILS.append("μονοτονία κυκλικής")
    check("τελευταία στάση στο τέλος", dists[-1], shape.total, 1.0)


def test_tracker_end_to_end():
    print("\n5) Πλήρης ροή: στίγματα → διελεύσεις με γνωστές ώρες")
    lat0 = 38.0
    _m_lat, m_lng = geo.latlng_scale(lat0)
    pts = [(lat0, 23.7), (lat0, 23.8)]
    total = 0.1 * m_lng                      # ~8783 m
    # Στάσεις ανά ~1/4 της διαδρομής
    stops = [(1, "S1", 0.0), (2, "S2", total * 0.25),
             (3, "S3", total * 0.50), (4, "S4", total * 0.75),
             (5, "S5", total)]
    g = FakeGeom(pts, stops)
    tr = GpsTracker({"TEST": g})

    base = datetime(2026, 7, 31, 10, 0, 0, tzinfo=timezone.utc)

    def fix(frac, secs):
        return {"VEH_NO": "12345",
                "CS_DATE": (base + timedelta(seconds=secs))
                           .astimezone(_athens()).strftime("%b %d %Y %I:%M:%S:000%p"),
                "CS_LAT": str(lat0), "CS_LNG": str(23.7 + 0.1 * frac)}

    def _athens():
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Athens")

    # Ρεαλιστικό διάστημα στιγμάτων (~40 s, όσο ανανεώνει ο ΟΑΣΑ).
    # Στίγμα 1: στην αρχή, t=0. Στίγμα 2: στο 60% της διαδρομής, t=40 s.
    # Σταθερή ταχύτητα ⇒ S2 (25%) στα 40·(0,25/0,60) = 16,667 s
    #                    S3 (50%) στα 40·(0,50/0,60) = 33,333 s
    tr.ingest("TEST", [fix(0.0, 0)])
    out = tr.ingest("TEST", [fix(0.6, 40)])

    got = {p["stop_code"]: (p["passed_at"] - base).total_seconds() for p in out}
    print(f"     διελεύσεις: { {k: round(v,2) for k,v in got.items()} }")
    if "S2" in got:
        check("S2 (25% της διαδρομής)", got["S2"], 40 * 0.25 / 0.6, 0.5)
    else:
        FAILS.append("λείπει S2")
        print("  ΛΑΘΟΣ λείπει η S2")
    if "S3" in got:
        check("S3 (50% της διαδρομής)", got["S3"], 40 * 0.50 / 0.6, 0.5)
    else:
        FAILS.append("λείπει S3")
        print("  ΛΑΘΟΣ λείπει η S3")
    if "S4" in got:
        FAILS.append("S4 δεν έπρεπε να περαστεί")
        print("  ΛΑΘΟΣ η S4 (75%) δεν έχει περαστεί ακόμη")
    else:
        print("  ΟΚ   η S4 σωστά ΔΕΝ καταγράφηκε (δεν έχει φτάσει)")

    # Το πλαφόν παρεμβολής: μεγάλο κενό ⇒ ΚΑΜΙΑ διέλευση, όχι εικασία.
    tr2 = GpsTracker({"TEST": FakeGeom(pts, stops)})
    tr2.ingest("TEST", [fix(0.0, 0)])
    out2 = tr2.ingest("TEST", [fix(0.6, 600)])     # 10′ κενό > MAX_INTERP_GAP_S
    cap_ok = not out2 and tr2.stats["gap_skips"] == 1
    print(f"  {'ΟΚ  ' if cap_ok else 'ΛΑΘΟΣ'} κενό 10′ δεν παρήγαγε διελεύσεις "
          f"(gap_skips={tr2.stats['gap_skips']})")
    if not cap_ok:
        FAILS.append("πλαφόν κενού")


def test_duplicate_and_lap():
    print("\n6) Διπλά στίγματα και νέα βόλτα")
    lat0 = 38.0
    _m, m_lng = geo.latlng_scale(lat0)
    pts = [(lat0, 23.7), (lat0, 23.8)]
    total = 0.1 * m_lng
    g = FakeGeom(pts, [(1, "S1", 0.0), (2, "S2", total * 0.5), (3, "S3", total)])
    tr = GpsTracker({"TEST": g})
    base = datetime(2026, 7, 31, 10, 0, 0, tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo
    ath = ZoneInfo("Europe/Athens")

    def fix(frac, secs):
        return {"VEH_NO": "9", "CS_DATE": (base + timedelta(seconds=secs))
                .astimezone(ath).strftime("%b %d %Y %I:%M:%S:000%p"),
                "CS_LAT": str(lat0), "CS_LNG": str(23.7 + 0.1 * frac)}

    tr.ingest("TEST", [fix(0.0, 0)])
    tr.ingest("TEST", [fix(0.0, 0)])          # ίδιο CS_DATE ⇒ διπλό
    dup_ok = tr.stats["dup"] == 1
    print(f"  {'ΟΚ  ' if dup_ok else 'ΛΑΘΟΣ'} διπλό στίγμα αγνοήθηκε "
          f"(dup={tr.stats['dup']})")
    if not dup_ok:
        FAILS.append("dedup")

    tr.ingest("TEST", [fix(0.9, 600)])        # προχώρησε
    before = tr.stats["passages"]
    tr.ingest("TEST", [fix(0.02, 900)])       # γύρισε στην αρχή ⇒ νέα βόλτα
    lap_ok = tr.stats["laps"] == 1 and tr.stats["passages"] == before
    print(f"  {'ΟΚ  ' if lap_ok else 'ΛΑΘΟΣ'} νέα βόλτα χωρίς ψεύτικες "
          f"διελεύσεις (laps={tr.stats['laps']}, "
          f"νέες διελεύσεις={tr.stats['passages']-before})")
    if not lap_ok:
        FAILS.append("lap reset")


def test_origin_terminus_emitted():
    """
    Το πιο σημαντικό σενάριο: το λεωφορείο ΞΕΚΙΝΑΕΙ από την αφετηρία και
    ΦΤΑΝΕΙ στο τερματικό. Και οι δύο πρέπει να καταγραφούν.

    Χωρίς τη μετατόπιση DEPART_EPS_M η αφετηρία (dist_m = 0) δεν διασχίζεται
    ΠΟΤΕ, γιατί δεν υπάρχει θέση πριν από το μηδέν — και το σύστημα χάνει
    ακριβώς την αναχώρηση.
    """
    print("\n8) Αφετηρία και τερματικό καταγράφονται")
    import db as _db
    import gps_tracker as gt

    lat0 = 38.0
    _m, m_lng = geo.latlng_scale(lat0)
    pts = [(lat0, 23.7), (lat0, 23.8)]
    total = 0.1 * m_lng

    class G(FakeGeom):
        def __init__(self):
            super().__init__(pts, [(1, "ORIG", 0.0), (2, "MID", total * 0.5),
                                   (3, "TERM", total)])
            # ίδια μετατόπιση που κάνει η πραγματική RouteGeometry
            self.stop_dists[0] += gt.DEPART_EPS_M
            self.stop_dists[-1] -= gt.ARRIVE_TOL_M

    tr = GpsTracker({"TEST": G()})
    base = datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo
    ath = ZoneInfo("Europe/Athens")

    def fix(frac, secs):
        return {"VEH_NO": "77", "CS_DATE": (base + timedelta(seconds=secs))
                .astimezone(ath).strftime("%b %d %Y %I:%M:%S:000%p"),
                "CS_LAT": str(lat0), "CS_LNG": str(23.7 + 0.1 * frac)}

    got = []
    # σταθμευμένο στην αφετηρία, μετά ξεκινά και διασχίζει όλη τη διαδρομή
    for frac, secs in [(0.000, 0), (0.001, 30), (0.10, 90), (0.35, 200),
                       (0.60, 320), (0.85, 450), (1.00, 560)]:
        got += tr.ingest("TEST", [fix(frac, secs)])

    codes = [p["stop_code"] for p in got]
    print(f"     καταγράφηκαν: {codes}")
    for want in ("ORIG", "MID", "TERM"):
        ok = want in codes
        print(f"  {'ΟΚ  ' if ok else 'ΛΑΘΟΣ'} {want}")
        if not ok:
            FAILS.append(f"λείπει {want}")

    if "ORIG" in codes:
        t = next(p["passed_at"] for p in got if p["stop_code"] == "ORIG")
        dep = (t - base).total_seconds()
        # Το όχημα ήταν ακίνητο ως τα 30 s και ξεκίνησε μετά· η αναχώρηση
        # πρέπει να πέσει στο διάστημα 30-90 s, κοντά στην αρχή της κίνησης.
        ok = 30 <= dep <= 90
        print(f"  {'ΟΚ  ' if ok else 'ΛΑΘΟΣ'} ώρα αναχώρησης {dep:.0f}s "
              f"(αναμένεται 30-90s, αφού το όχημα ξεκίνησε στα 30s)")
        if not ok:
            FAILS.append("ώρα αναχώρησης")


def test_cs_date():
    print("\n7) Ανάλυση CS_DATE (ώρα Αθήνας → UTC)")
    dt = _parse_cs_date("Jul 31 2026 04:03:25:000PM")
    # Ιούλιος = θερινή ώρα Αθήνας (UTC+3) ⇒ 16:03:25 τοπική = 13:03:25 UTC
    ok = dt is not None and dt.hour == 13 and dt.minute == 3 and dt.second == 25
    print(f"  {'ΟΚ  ' if ok else 'ΛΑΘΟΣ'} 04:03:25PM Αθήνα → {dt} UTC")
    if not ok:
        FAILS.append("CS_DATE")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    test_scale()
    test_straight_line()
    test_interpolation()
    test_monotonic_snap()
    test_tracker_end_to_end()
    test_duplicate_and_lap()
    test_origin_terminus_emitted()
    test_cs_date()
    print("\n" + "=" * 60)
    if FAILS:
        print(f"ΑΠΕΤΥΧΑΝ {len(FAILS)}: {', '.join(FAILS)}")
        sys.exit(1)
    print("Όλοι οι έλεγχοι πέρασαν.")
