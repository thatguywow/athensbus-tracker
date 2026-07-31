"""
compare_methods.py — GPS vs ανίχνευση-εξαφάνισης, στα ΙΔΙΑ δρομολόγια.

Η ερώτηση δεν είναι «ποια μέθοδος ακούγεται καλύτερη» αλλά «πόσο διαφέρουν
στην πράξη, και ποια λέει την αλήθεια». Και οι δύο γράφουν στον ίδιο πίνακα
stop_passages με διαφορετικό `method`, οπότε συγκρίνονται απευθείας.

ΤΙ ΜΕΤΡΑΜΕ
==========
1. ΚΑΛΥΨΗ      πόσες διελεύσεις παράγει η καθεμιά, σε πόσες στάσεις, πόσα οχήματα
2. ΣΥΜΦΩΝΙΑ    στις ΚΟΙΝΕΣ στάσεις: κατανομή της διαφοράς (GPS − εξαφάνιση)
3. ΜΕΡΟΛΗΨΙΑ   το πρόσημο της διαφοράς. Η ανίχνευση εξαφάνισης καρφώνει τη
               διέλευση μέσα στο παράθυρο [τελευταία θέαση, πρώτη απουσία],
               που είναι ένας ΟΛΟΚΛΗΡΟΣ κύκλος δημοσκόπησης πλατύ, και το
               btime2 είναι στρογγυλεμένο σε ακέραια λεπτά. Αν υπάρχει
               συστηματική μεροληψία, εδώ φαίνεται.
4. ΦΥΣΙΚΗ      ταχύτητες που υπονοούνται ανάμεσα σε διαδοχικές στάσεις. Καμία
               από τις δύο δεν είναι «αλήθεια» εξ ορισμού — αλλά μια μέθοδος
               που παράγει λεωφορεία στα 300 km/h ή αρνητικούς χρόνους
               αυτοαναιρείται.

    python scripts/compare_methods.py [YYYY-MM-DD]
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db

# Δύο διελεύσεις της ίδιας στάσης/οχήματος πιο κοντά από αυτό θεωρούνται η ΙΔΙΑ
# φυσική διέλευση, ιδωμένη από τις δύο μεθόδους. Πιο μακριά, είναι άλλη βόλτα.
MATCH_WINDOW_S = 900.0


def _p(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def overlap_window(conn, service_date: str) -> tuple[str, str]:
    """
    Το χρονικό παράθυρο όπου έτρεχαν ΚΑΙ ΟΙ ΔΥΟ μέθοδοι.

    Χωρίς αυτό η σύγκριση κάλυψης είναι άδικη προς όποια μέθοδο ξεκίνησε
    αργότερα ή σταμάτησε νωρίτερα — και θα έδειχνε διαφορά εκεί που υπάρχει
    μόνο διαφορετική διάρκεια καταγραφής.
    """
    row = conn.execute("""
        SELECT MAX(mn) s, MIN(mx) e FROM (
            SELECT MIN(passed_at) mn, MAX(passed_at) mx
            FROM stop_passages WHERE service_date=? AND method='gps'
            UNION ALL
            SELECT MIN(passed_at), MAX(passed_at)
            FROM stop_passages WHERE service_date=?
              AND COALESCE(method,'disappearance')='disappearance')
    """, (service_date, service_date)).fetchone()
    return (row["s"], row["e"]) if row and row["s"] and row["e"] else (None, None)


def coverage(conn, service_date: str, win=(None, None)):
    print("\n" + "=" * 78)
    print("1) ΚΑΛΥΨΗ")
    print("=" * 78)
    if win[0]:
        mins = (_p(win[1]) - _p(win[0])).total_seconds() / 60
        print(f"Κοινό παράθυρο: {win[0][11:19]} → {win[1][11:19]} ({mins:.0f} λεπτά)\n")
        print(f"{'μέθοδος':<16} {'διελεύσεις':>11} {'ανά λεπτό':>10} {'στάσεις':>9} "
              f"{'οχήματα':>9} {'διαδρομές':>10}")
        print("-" * 78)
        for r in conn.execute("""
                SELECT COALESCE(method,'disappearance') m, COUNT(*) n,
                       COUNT(DISTINCT stop_code) s, COUNT(DISTINCT vehicle_no) v,
                       COUNT(DISTINCT route_code) rt
                FROM stop_passages
                WHERE service_date=? AND passed_at>=? AND passed_at<=?
                GROUP BY 1 ORDER BY n DESC""", (service_date, win[0], win[1])):
            print(f"{r['m']:<16} {r['n']:>11} {r['n']/mins:>10.1f} {r['s']:>9} "
                  f"{r['v']:>9} {r['rt']:>10}")
        print()
        print("Σύνολο καταγραφής (όχι κοινό παράθυρο):")
    print(f"{'μέθοδος':<16} {'διελεύσεις':>11} {'στάσεις':>9} {'οχήματα':>9} "
          f"{'διαδρομές':>10} {'διελ./όχημα':>12}")
    print("-" * 78)
    for r in conn.execute("""
            SELECT method, COUNT(*) n, COUNT(DISTINCT stop_code) s,
                   COUNT(DISTINCT vehicle_no) v, COUNT(DISTINCT route_code) rt
            FROM stop_passages WHERE service_date=? GROUP BY method
            ORDER BY n DESC""", (service_date,)):
        per = r["n"] / r["v"] if r["v"] else 0
        print(f"{r['method']:<16} {r['n']:>11} {r['s']:>9} {r['v']:>9} "
              f"{r['rt']:>10} {per:>12.1f}")

    # Η ανίχνευση εξαφάνισης βλέπει ΜΟΝΟ τις ακραίες στάσεις. Το GPS βλέπει
    # όλη τη διαδρομή — εκεί είναι η αλλαγή που δεν φαίνεται σε καμία μέση τιμή.
    print(f"\n{'':16} {'άκρα (origin/terminus/near)':>32} {'μέση διαδρομή':>16}")
    for m in ("disappearance", "gps"):
        edge = conn.execute("""
            SELECT COUNT(*) c FROM stop_passages WHERE service_date=? AND method=?
              AND stop_type IN ('origin','near_origin','near_terminus','terminus')
        """, (service_date, m)).fetchone()["c"]
        mid = conn.execute("""
            SELECT COUNT(*) c FROM stop_passages WHERE service_date=? AND method=?
              AND stop_type = 'middle'""", (service_date, m)).fetchone()["c"]
        print(f"{m:<16} {edge:>32} {mid:>16}")


def agreement(conn, service_date: str, win=(None, None)):
    print("\n" + "=" * 78)
    print("2/3) ΣΥΜΦΩΝΙΑ ΚΑΙ ΜΕΡΟΛΗΨΙΑ στις κοινές στάσεις")
    print("=" * 78)

    rows = conn.execute("""
        SELECT route_code, stop_code, stop_order, vehicle_no, passed_at,
               COALESCE(method,'disappearance') method
        FROM stop_passages WHERE service_date=?
          AND COALESCE(method,'disappearance') IN ('gps','disappearance')
          AND (? IS NULL OR passed_at >= ?)
          AND (? IS NULL OR passed_at <= ?)
        ORDER BY route_code, stop_code, vehicle_no, passed_at
    """, (service_date, win[0], win[0], win[1], win[1])).fetchall()

    by_key: dict[tuple, dict[str, list]] = defaultdict(lambda: {"gps": [], "disappearance": []})
    for r in rows:
        key = (r["route_code"], r["stop_code"], r["stop_order"], r["vehicle_no"])
        by_key[key][r["method"]].append(_p(r["passed_at"]))

    deltas: list[float] = []
    per_route: dict[str, list[float]] = defaultdict(list)
    matched = only_gps = only_dis = 0

    for key, m in by_key.items():
        g, d = sorted(m["gps"]), sorted(m["disappearance"])
        if not g and not d:
            continue
        if not g:
            only_dis += len(d); continue
        if not d:
            only_gps += len(g); continue
        used = set()
        for dt in d:
            best, best_i = None, None
            for i, gt in enumerate(g):
                if i in used:
                    continue
                diff = (gt - dt).total_seconds()
                if abs(diff) <= MATCH_WINDOW_S and (best is None or abs(diff) < abs(best)):
                    best, best_i = diff, i
            if best is None:
                only_dis += 1
            else:
                used.add(best_i)
                deltas.append(best)
                per_route[key[0]].append(best)
                matched += 1
        only_gps += len(g) - len(used)

    print(f"ζεύγη που ταιριάχτηκαν: {matched}")
    print(f"μόνο GPS: {only_gps}    μόνο εξαφάνιση: {only_dis}")
    if not deltas:
        print("\n(δεν υπάρχουν κοινές διελεύσεις ακόμη — χρειάζεται μεγαλύτερο δείγμα)")
        return

    deltas.sort()
    n = len(deltas)

    def q(p):
        return deltas[min(n - 1, int(n * p))]

    print(f"\nΔιαφορά GPS − εξαφάνιση, σε δευτερόλεπτα (θετικό = η εξαφάνιση "
          f"τοποθετεί τη διέλευση ΝΩΡΙΤΕΡΑ):")
    print(f"  n={n}  διάμεσος={statistics.median(deltas):+.1f}s  "
          f"μέση={statistics.mean(deltas):+.1f}s")
    print(f"  p05={q(0.05):+.0f}s  p25={q(0.25):+.0f}s  p75={q(0.75):+.0f}s  "
          f"p95={q(0.95):+.0f}s")
    absd = sorted(abs(x) for x in deltas)
    print(f"  |διαφορά|: διάμεσος={statistics.median(absd):.0f}s  "
          f"p90={absd[int(n*0.9)]:.0f}s  max={absd[-1]:.0f}s")
    within = lambda t: 100.0 * sum(1 for x in absd if x <= t) / n
    print(f"  εντός ±30s: {within(30):.0f}%   ±60s: {within(60):.0f}%   "
          f"±120s: {within(120):.0f}%")

    if abs(statistics.median(deltas)) > 10:
        who = "εξαφάνιση ΝΩΡΙΤΕΡΑ" if statistics.median(deltas) > 0 else "εξαφάνιση ΑΡΓΟΤΕΡΑ"
        print(f"\n  ⚠ ΣΥΣΤΗΜΑΤΙΚΗ ΜΕΡΟΛΗΨΙΑ: {who} κατά "
              f"~{abs(statistics.median(deltas)):.0f}s κατά μέσο όρο.")

    if len(per_route) > 1:
        print(f"\n  ανά διαδρομή (διάμεσος διαφοράς):")
        for rc, ds in sorted(per_route.items(), key=lambda kv: -len(kv[1]))[:10]:
            if len(ds) >= 3:
                print(f"    {rc}: n={len(ds):>3}  διάμεσος={statistics.median(ds):+.0f}s")


def physics(conn, service_date: str):
    print("\n" + "=" * 78)
    print("4) ΦΥΣΙΚΗ ΕΥΛΟΓΟΦΑΝΕΙΑ — ταχύτητες ανάμεσα σε διαδοχικές στάσεις")
    print("=" * 78)

    offs = defaultdict(dict)
    for r in conn.execute("SELECT route_code, stop_order, dist_m FROM stop_shape_offsets"):
        offs[r["route_code"]][r["stop_order"]] = r["dist_m"]

    print(f"{'μέθοδος':<16} {'τμήματα':>9} {'διάμ. km/h':>11} {'p95 km/h':>10} "
          f"{'>80km/h':>9} {'αρνητικά':>10}")
    print("-" * 78)
    for method in ("gps", "disappearance"):
        rows = conn.execute("""
            SELECT route_code, vehicle_no, stop_order, passed_at
            FROM stop_passages WHERE service_date=? AND method=?
            ORDER BY route_code, vehicle_no, passed_at""",
            (service_date, method)).fetchall()
        speeds, neg = [], 0
        prev_key, prev = None, None
        for r in rows:
            key = (r["route_code"], r["vehicle_no"])
            cur = (r["stop_order"], _p(r["passed_at"]))
            if prev_key == key and prev is not None:
                d0 = offs.get(r["route_code"], {}).get(prev[0])
                d1 = offs.get(r["route_code"], {}).get(cur[0])
                dt = (cur[1] - prev[1]).total_seconds()
                if d0 is not None and d1 is not None and d1 > d0 and 0 < dt < 1800:
                    speeds.append((d1 - d0) / dt * 3.6)
                elif d0 is not None and d1 is not None and d1 > d0 and dt <= 0:
                    neg += 1
            prev_key, prev = key, cur
        if not speeds:
            print(f"{method:<16} {'—':>9}")
            continue
        speeds.sort()
        fast = sum(1 for s in speeds if s > 80)
        print(f"{method:<16} {len(speeds):>9} {statistics.median(speeds):>11.1f} "
              f"{speeds[int(len(speeds)*0.95)]:>10.1f} "
              f"{100*fast/len(speeds):>8.1f}% {neg:>10}")
    print("\n  Αστικό λεωφορείο: ρεαλιστική διάμεσος ~15-25 km/h. Ποσοστό >80 km/h "
          "\n  ή αρνητικοί χρόνοι = η μέθοδος παράγει φυσικά αδύνατα αποτελέσματα.")


def reconstruct_comparison(conn, service_date: str):
    """
    Τρέχει την ΑΝΑΚΑΤΑΣΚΕΥΗ με κάθε πηγή και συγκρίνει τα ΔΡΟΜΟΛΟΓΙΑ.

    Οι διελεύσεις είναι το ενδιάμεσο· αυτό που βλέπει ο χρήστης είναι
    αναχώρηση/λήξη/διάρκεια. Εδώ μετράμε αυτά.

    ΠΡΟΣΟΧΗ: η ανακατασκευή σβήνει και ξαναγράφει τον πίνακα trips, οπότε οι
    τρεις εκδοχές τρέχουν διαδοχικά και κρατιέται στιγμιότυπο μετά από κάθε μία.
    Χρειάζεται ΠΛΗΡΗ ημέρα για να έχει νόημα: ένα δρομολόγιο θέλει 40-90 λεπτά,
    οπότε σε παράθυρο μιας ώρας τα περισσότερα είναι ημιτελή εξ ορισμού.
    """
    from trip_reconstruction_passages import reconstruct_route_day_from_passages
    from audit_day import run_audit

    print("\n" + "=" * 78)
    print("5) ΑΝΑΚΑΤΑΣΚΕΥΗ ΔΡΟΜΟΛΟΓΙΩΝ ανά πηγή διελεύσεων")
    print("=" * 78)

    routes = [r["route_code"] for r in conn.execute("SELECT route_code FROM routes")]
    computed_at = db.now_utc_iso()
    results = {}

    for source in ("disappearance", "gps", "both"):
        for rc in routes:
            try:
                reconstruct_route_day_from_passages(conn, rc, service_date,
                                                    computed_at, source=source)
            except Exception as e:
                print(f"   {rc} ({source}): {e}")
        conn.commit()

        row = conn.execute("""
            SELECT COUNT(*) n,
                   SUM(terminus_arrived_at IS NOT NULL) arr,
                   COUNT(DISTINCT vehicle_no) veh
            FROM trips WHERE service_date=?""", (service_date,)).fetchone()
        dep = conn.execute("""
            SELECT COUNT(*) c FROM trips t WHERE t.service_date=? AND EXISTS (
              SELECT 1 FROM trip_stop_times x WHERE x.trip_id=t.id
                AND x.stop_order = (SELECT MIN(stop_order) FROM stops s
                                    WHERE s.route_code=t.route_code))
        """, (service_date,)).fetchone()["c"]
        pts = conn.execute("""
            SELECT AVG(stop_count) a FROM trips WHERE service_date=?""",
            (service_date,)).fetchone()["a"]
        try:
            audit = run_audit(conn, service_date, computed_at)
            conn.commit()
        except Exception:
            audit = {}
        results[source] = {
            "trips": row["n"] or 0,
            "with_arrival": row["arr"] or 0,
            "with_measured_dep": dep,
            "vehicles": row["veh"] or 0,
            "avg_points": pts or 0,
            "audit": sum(audit.values()) if audit else 0,
            "audit_detail": audit,
        }

    print(f"{'πηγή':<16} {'δρομ.':>7} {'μετρημ. αναχ.':>14} {'με λήξη':>9} "
          f"{'σημεία/δρομ.':>13} {'αδύνατα':>9}")
    print("-" * 78)
    for src, r in results.items():
        n = r["trips"] or 1
        print(f"{src:<16} {r['trips']:>7} "
              f"{r['with_measured_dep']:>7} ({100*r['with_measured_dep']/n:>3.0f}%) "
              f"{r['with_arrival']:>4} ({100*r['with_arrival']/n:>3.0f}%) "
              f"{r['avg_points']:>13.1f} {r['audit']:>9}")

    types = sorted({k for r in results.values() for k in r["audit_detail"]})
    if types:
        print(f"\n  αδύνατα αποτελέσματα ανά τύπο:")
        print(f"    {'τύπος':<26} " + " ".join(f"{s:>14}" for s in results))
        for t in types:
            vals = " ".join(f"{results[s]['audit_detail'].get(t,0):>14}" for s in results)
            print(f"    {t:<26} {vals}")

    # Αφήνουμε τη βάση στην ΠΡΟΕΠΙΛΕΓΜΕΝΗ κατάσταση, όχι στην τελευταία που έτυχε.
    for rc in routes:
        try:
            reconstruct_route_day_from_passages(conn, rc, service_date,
                                                computed_at, source="disappearance")
        except Exception:
            pass
    conn.commit()
    print("\n  (η βάση επαναφέρθηκε στην πηγή 'disappearance')")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    service_date = args[0] if args else db.athens_service_date()
    conn = db.get_connection()
    print(f"Ημέρα βάρδιας: {service_date}")
    win = overlap_window(conn, service_date)
    coverage(conn, service_date, win)
    agreement(conn, service_date, win)
    physics(conn, service_date)
    if "--reconstruct" in sys.argv:
        reconstruct_comparison(conn, service_date)
    else:
        print("\n(πρόσθεσε --reconstruct για σύγκριση σε επίπεδο δρομολογίων· "
              "θέλει πλήρη ημέρα δεδομένων)")
    conn.close()


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    main()
