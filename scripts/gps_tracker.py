"""
gps_tracker.py — διελεύσεις από στίγματα GPS, με ροή (streaming).

ΤΙ ΑΛΛΑΖΕΙ ΣΕ ΣΧΕΣΗ ΜΕ ΤΗΝ ΑΝΙΧΝΕΥΣΗ ΕΞΑΦΑΝΙΣΗΣ
=================================================
Η υπάρχουσα μέθοδος συμπεραίνει ότι ένα λεωφορείο πέρασε μια στάση επειδή η
ΠΡΟΒΛΕΨΗ του έπαψε να εμφανίζεται. Αυτό είναι έμμεσο (μετράμε την πρόβλεψη του
ΟΑΣΑ, όχι το λεωφορείο), στρογγυλεμένο (το btime2 είναι ακέραια λεπτά) και
δουλεύει μόνο στις 6 ακραίες στάσεις κάθε διαδρομής.

Εδώ βλέπουμε το ίδιο το όχημα. Κάθε στίγμα προβάλλεται στην πολυγραμμή και
γίνεται ένας αριθμός: πόσα μέτρα έχει διανύσει. Όταν ο αριθμός αυτός περάσει
τη θέση μιας στάσης, η ώρα διέλευσης προκύπτει με γραμμική παρεμβολή ανάμεσα
στα δύο στίγματα. Αποτέλεσμα: ΟΛΕΣ οι στάσεις, όχι 6 — και σφάλμα λίγων
δευτερολέπτων αντί για ±30-60 s.

ΓΙΑΤΙ ΔΕΝ ΑΠΟΘΗΚΕΥΟΥΜΕ ΟΛΑ ΤΑ ΣΤΙΓΜΑΤΑ
=======================================
~1.100 οχήματα × ~1,7 στίγματα/λεπτό × ~20 ώρες ≈ 2,3 εκατ. σειρές/ημέρα,
δηλαδή ~340 MB/ημέρα με τα ευρετήρια. Σε φθηνό VPS αυτό δεν κρατιέται. Η
επεξεργασία γίνεται λοιπόν ΣΤΗ ΡΟΗ, με λίγα KB κατάστασης στη μνήμη, και
αποθηκεύεται μόνο το παράγωγο: οι διελεύσεις (~600k/ημέρα, φραγμένο).
Τα ακατέργαστα στίγματα κρατιούνται προαιρετικά και για λίγες ώρες, μόνο για
έλεγχο (--store-pings).

ΤΟ ΟΡΙΟ ΡΥΘΜΟΥ ΕΙΝΑΙ ΚΟΙΝΟ
==========================
Ο περιορισμός του ΟΑΣΑ φαίνεται να είναι ανά IP, όχι ανά endpoint: στο probe
ΚΑΙ τα δύο endpoints άρχισαν να βγάζουν 403 γύρω στα ίδια req/s. Άρα ο GPS
poller ΔΕΝ φτιάχνει δικό του budget — δέχεται τον RateLimiter του καλούντος
και μοιράζεται το ίδιο.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import geo
import oasa_client as oasa

log = logging.getLogger("gps_tracker")

try:
    from zoneinfo import ZoneInfo
    _ATHENS = ZoneInfo("Europe/Athens")
except ImportError:                                   # pragma: no cover
    _ATHENS = None

# ── Παράμετροι ──────────────────────────────────────────────────────────────

# Πάνω από αυτό το κενό ανάμεσα σε δύο στίγματα, η παρεμβολή γίνεται εικασία:
# το όχημα μπορεί να στάθηκε, να έκανε παράκαμψη, ή να έσβησε ο πομπός. Οι
# ενδιάμεσες στάσεις ΔΕΝ καταγράφονται τότε — προτιμούμε κενό από ψεύτικη
# ακρίβεια. Στα κανονικά ~35 s ανανέωσης αυτό δεν ενεργοποιείται σχεδόν ποτέ.
MAX_INTERP_GAP_S = 240.0

# Οπισθοχώρηση πάνω από αυτό ⇒ νέα βόλτα (το όχημα γύρισε στην αφετηρία), όχι
# θόρυβος GPS. Κάτω από αυτό είναι απλώς ανακρίβεια στίγματος και αγνοείται.
LAP_RESET_M = 1500.0

# Πόσο κρατάμε στη μνήμη ένα όχημα που έπαψε να εμφανίζεται.
STATE_TTL_S = 3600.0

# Ελάχιστη μετακίνηση για να θεωρηθεί ότι το όχημα προχώρησε. Κάτω από αυτό
# είναι θόρυβος GPS σε στάθμευση — και θα παρήγαγε δεκάδες ψεύτικες διελεύσεις
# από ένα παρκαρισμένο λεωφορείο δίπλα σε στάση.
MIN_ADVANCE_M = 5.0

# Πάνω από αυτό, η μετακίνηση δεν είναι λεωφορείο. Τα αστικά πιάνουν 80-90 km/h
# μόνο σε λεωφόρους/Αττική Οδό, οπότε τα 100 αφήνουν περιθώριο και εξακολουθούν
# να κόβουν τα σφάλματα προβολής, που εμφανίζονται με εκατοντάδες ή χιλιάδες.
MAX_SPEED_KMH = 100.0

# ── Αφετηρία και τερματικό: οι δύο στάσεις που έχουν πραγματικά σημασία ──────
# Η αφετηρία κάθεται στο dist_m = 0 της πολυγραμμής. Ο κανόνας διέλευσης απαιτεί
# prev_dist < stop_dist <= dist, που στο μηδέν ΔΕΝ ΜΠΟΡΕΙ να ισχύσει ποτέ: δεν
# υπάρχει θέση πριν από την αρχή. Αποτέλεσμα, μετρημένο: 1 διέλευση σε άκρο
# στάση στις 190 — δηλαδή χάναμε ακριβώς την αναχώρηση, το πιο σημαντικό
# μέγεθος όλου του συστήματος.
#
# Λύση: η αφετηρία μετράει λίγα μέτρα ΜΕΤΑ την ονομαστική της θέση, ώστε το
# όχημα που ξεκινά να την «περνά» κανονικά. Το τίμημα είναι μια γνωστή, μικρή
# καθυστέρηση: στα 25 m με ταχύτητα εκκίνησης ~20 km/h μιλάμε για ~4-5
# δευτερόλεπτα — ασήμαντο μπροστά στα ±30-60 s της ανίχνευσης εξαφάνισης, και
# σταθερό (άρα αφαιρέσιμο), όχι τυχαίο.
DEPART_EPS_M = 25.0

# Συμμετρικά στο τερματικό: τα λεωφορεία σταματούν λίγο πριν από το ονομαστικό
# σημείο, και το τελευταίο στίγμα πριν σβήσει ο πομπός σπάνια πέφτει ακριβώς
# πάνω του. Χωρίς ανοχή, η άφιξη απλώς δεν καταγράφεται.
ARRIVE_TOL_M = 25.0

# Πόσο κοντά στα άκρα μετράει ως «κοντά στην αφετηρία/τερματικό». Ίδιο με το
# EDGE_DEPTH του local_poller, ώστε οι δύο μέθοδοι να χαρακτηρίζουν τις στάσεις
# με τον ΙΔΙΟ τρόπο και να συγκρίνονται κατηγορία προς κατηγορία.
EDGE_DEPTH = 3


def _parse_cs_date(raw: str) -> datetime | None:
    """'Jul 31 2026 04:03:25:000PM' (ώρα Αθήνας) → aware UTC datetime."""
    try:
        naive = datetime.strptime(raw.strip(), "%b %d %Y %I:%M:%S:%f%p")
    except (ValueError, AttributeError):
        return None
    if _ATHENS is not None:
        return naive.replace(tzinfo=_ATHENS).astimezone(timezone.utc)
    return naive.replace(tzinfo=timezone.utc)


class RouteGeometry:
    """Πολυγραμμή + θέσεις στάσεων μιας διαδρομής, φορτωμένα μία φορά."""

    __slots__ = ("route_code", "shape", "stop_dists", "stop_codes",
                 "stop_orders", "usable")

    def __init__(self, conn, route_code: str, max_snap_m: float = 150.0):
        self.route_code = route_code
        self.usable = False
        self.shape = None
        self.stop_dists: list[float] = []
        self.stop_codes: list[str] = []
        self.stop_orders: list[int] = []

        pts = conn.execute(
            "SELECT lat, lng, dist_m FROM route_shapes WHERE route_code=? "
            "ORDER BY seq", (route_code,)).fetchall()
        if len(pts) < 2:
            return
        offs = conn.execute(
            "SELECT stop_order, stop_code, dist_m, snap_err_m "
            "FROM stop_shape_offsets WHERE route_code=? ORDER BY dist_m",
            (route_code,)).fetchall()
        if not offs:
            return
        # Μια διαδρομή όπου οι στάσεις δεν κάθονται στην πολυγραμμή δεν είναι
        # αξιόπιστη: καλύτερα να μείνει στην παλιά μέθοδο παρά να βγάλει
        # σίγουρες αλλά λάθος ώρες.
        worst = max((o["snap_err_m"] for o in offs
                     if o["snap_err_m"] is not None), default=0.0)
        if worst > max_snap_m:
            log.debug("%s: παραλείπεται, snap έως %.0f m", route_code, worst)
            return

        self.shape = geo.RouteShape([(p["lat"], p["lng"]) for p in pts],
                                    dists=[p["dist_m"] for p in pts])
        for o in offs:
            self.stop_dists.append(o["dist_m"])
            self.stop_codes.append(o["stop_code"])
            self.stop_orders.append(o["stop_order"])

        # Μετατόπιση των δύο άκρων ώστε να είναι ΔΙΑΣΧΙΣΙΜΑ (βλ. DEPART_EPS_M).
        # Γίνεται μόνο αν δεν καταπίνει τη γειτονική στάση — σε πολύ πυκνές
        # αστικές διαδρομές οι πρώτες στάσεις απέχουν λίγες δεκάδες μέτρα.
        if len(self.stop_dists) >= 2:
            if self.stop_dists[0] + DEPART_EPS_M < self.stop_dists[1]:
                self.stop_dists[0] += DEPART_EPS_M
            if self.stop_dists[-1] - ARRIVE_TOL_M > self.stop_dists[-2]:
                self.stop_dists[-1] -= ARRIVE_TOL_M
        self.usable = True

    def stops_between(self, d0: float, d1: float):
        """Στάσεις με d0 < dist <= d1, με τη σειρά."""
        out = []
        for i, d in enumerate(self.stop_dists):
            if d0 < d <= d1:
                out.append((self.stop_orders[i], self.stop_codes[i], d))
            elif d > d1:
                break
        return out


class GpsTracker:
    """
    Κρατά την κατάσταση κάθε οχήματος και βγάζει διελεύσεις.

    Δεν αγγίζει δίκτυο και δεν αγγίζει βάση: παίρνει στίγματα, επιστρέφει
    διελεύσεις. Έτσι δοκιμάζεται με συνθετικά δεδομένα χωρίς τίποτα ζωντανό.
    """

    def __init__(self, geometries: dict[str, RouteGeometry]):
        self.geom = geometries
        # (route_code, vehicle) → {"dist", "ts", "seen"}
        self.state: dict[tuple[str, str], dict] = {}
        self.stats = {"fixes": 0, "dup": 0, "unprojectable": 0,
                      "passages": 0, "laps": 0, "gap_skips": 0,
                      "impossible": 0}

    def prune(self, now_mono: float):
        dead = [k for k, v in self.state.items()
                if now_mono - v["seen"] > STATE_TTL_S]
        for k in dead:
            del self.state[k]

    def ingest(self, route_code: str, fixes: list[dict]) -> list[dict]:
        """
        Δέχεται την απάντηση του getBusLocation για μία διαδρομή.
        Επιστρέφει τις νέες διελεύσεις που προέκυψαν.
        """
        g = self.geom.get(route_code)
        if g is None or not g.usable:
            return []

        out: list[dict] = []
        now_mono = time.monotonic()

        for f in fixes:
            veh = str(f.get("VEH_NO") or "").strip()
            if not veh:
                continue
            ts = _parse_cs_date(f.get("CS_DATE") or "")
            if ts is None:
                continue
            try:
                lat = float(f["CS_LAT"])
                lng = float(f["CS_LNG"])
            except (KeyError, TypeError, ValueError):
                continue

            self.stats["fixes"] += 1
            key = (route_code, veh)
            prev = self.state.get(key)

            # Ρωτάμε πιο συχνά από όσο ανανεώνει ο ΟΑΣΑ (~35 s), οπότε το ίδιο
            # στίγμα επιστρέφεται πολλές φορές. Χωρίς αυτό, κάθε επανάληψη θα
            # μετρούσε ως «νέα» μέτρηση με μηδενική κίνηση.
            if prev is not None and ts <= prev["ts"]:
                self.stats["dup"] += 1
                prev["seen"] = now_mono
                continue

            near = prev["dist"] if prev is not None else None
            proj = g.shape.project(lat, lng, near_dist=near)
            if proj is None:
                self.stats["unprojectable"] += 1
                continue
            dist, _err = proj

            if prev is not None:
                gap_s = (ts - prev["ts"]).total_seconds()
                advance = dist - prev["dist"]
                kmh = (abs(advance) / gap_s * 3.6) if gap_s > 0 else 0.0

                if advance < -LAP_RESET_M or advance > g.shape.total / 2:
                    # Επέστρεψε στην αρχή ⇒ νέα βόλτα. Καμία διέλευση δεν
                    # παράγεται «προς τα πίσω».
                    #
                    # ΡΑΦΗ ΚΥΚΛΙΚΗΣ: σε κυκλική διαδρομή το 0 και το ΜΗΚΟΣ είναι
                    # το ΙΔΙΟ φυσικό σημείο. Όχημα που κινείται με φθίνουσα
                    # απόσταση και περνά τη ραφή φαίνεται να ΠΗΔΑΕΙ ΜΠΡΟΣΤΑ σχεδόν
                    # ολόκληρη τη διαδρομή. Μετρημένο στη γραμμή 5366 (κυκλική,
                    # 33,2 km): άλμα +29.860 m σε 90 s — 1.194 km/h — που παρήγαγε
                    # 53 ΨΕΥΤΙΚΕΣ διελεύσεις από μία μετάβαση. Άλμα μεγαλύτερο από
                    # μισή διαδρομή είναι πάντα αναδίπλωση ραφής, ποτέ πραγματική
                    # κίνηση.
                    self.stats["laps"] += 1
                elif kmh > MAX_SPEED_KMH:
                    # Ό,τι δεν έπιασε ο έλεγχος ραφής: σφάλμα προβολής, GPS που
                    # ξεφεύγει, ή όχημα που άλλαξε διαδρομή. Δεν παράγουμε
                    # διελεύσεις — απλώς επαναπροσδιορίζουμε τη θέση.
                    self.stats["impossible"] += 1
                elif gap_s > MAX_INTERP_GAP_S:
                    self.stats["gap_skips"] += 1
                elif advance >= MIN_ADVANCE_M:
                    t0 = prev["ts"].timestamp()
                    t1 = ts.timestamp()
                    for order, code, sd in g.stops_between(prev["dist"], dist):
                        secs = geo.interpolate_crossing(
                            prev["dist"], t0, dist, t1, sd)
                        passed = datetime.fromtimestamp(secs, tz=timezone.utc)
                        out.append({
                            "route_code":  route_code,
                            "stop_code":   code,
                            "stop_order":  order,
                            "vehicle_no":  veh,
                            "passed_at":   passed,
                        })
                        self.stats["passages"] += 1

            self.state[key] = {"dist": dist, "ts": ts, "seen": now_mono}

        if len(self.state) > 5000:
            self.prune(now_mono)
        return out


def load_geometries(conn, route_codes: list[str] | None = None
                    ) -> dict[str, RouteGeometry]:
    if route_codes is None:
        route_codes = [r["route_code"] for r in
                       conn.execute("SELECT route_code FROM routes")]
    out = {}
    skipped = 0
    for rc in route_codes:
        g = RouteGeometry(conn, rc)
        if g.usable:
            out[rc] = g
        else:
            skipped += 1
    log.info("Γεωμετρία: %d διαδρομές έτοιμες, %d χωρίς χρησιμοποιήσιμο σχήμα",
             len(out), skipped)
    return out


# ── Ενσωμάτωση με poller ────────────────────────────────────────────────────

def stop_type_for(order: int, lo: int, hi: int) -> str:
    """Ίδιες κατηγορίες με τον local_poller, ώστε οι δύο μέθοδοι να συγκρίνονται."""
    if order == lo:
        return "origin"
    if order == hi:
        return "terminus"
    if order < lo + EDGE_DEPTH:
        return "near_origin"
    if order > hi - EDGE_DEPTH:
        return "near_terminus"
    return "middle"


def write_passages(conn, passages: list[dict], bounds: dict,
                   recorded_at: str) -> int:
    """Γράφει διελεύσεις GPS. method='gps' ώστε να ξεχωρίζουν στη σύγκριση."""
    n = 0
    for p in passages:
        lo, hi = bounds.get(p["route_code"], (None, None))
        stype = (stop_type_for(p["stop_order"], lo, hi)
                 if lo is not None else "middle")
        passed_iso = p["passed_at"].isoformat()
        try:
            c = conn.execute("""
                INSERT OR IGNORE INTO stop_passages
                    (route_code, stop_code, stop_type, stop_order, vehicle_no,
                     passed_at, service_date, recorded_at, method)
                VALUES (?,?,?,?,?,?,?,?,'gps')
            """, (p["route_code"], p["stop_code"], stype, p["stop_order"],
                  p["vehicle_no"], passed_iso,
                  db.athens_service_date(p["passed_at"]), recorded_at))
            n += c.rowcount
        except Exception as e:
            log.debug("write_passages: %s", e)
    return n


def route_bounds(conn) -> dict:
    return {r["route_code"]: (r["lo"], r["hi"]) for r in conn.execute(
        "SELECT route_code, MIN(stop_order) lo, MAX(stop_order) hi "
        "FROM stops GROUP BY route_code")}


def run(rate: float = 18.0, duration_s: float | None = None,
        route_codes: list[str] | None = None, store_pings: bool = False,
        limiter=None, stop_event: threading.Event | None = None):
    """
    Βρόχος GPS: round-robin σε όλες τις διαδρομές, με όριο ρυθμού.

    rate: κλήσεις/δευτ. ΑΠΟΚΛΕΙΣΤΙΚΑ για το getBusLocation, αν δεν δοθεί κοινός
    limiter. Στο δίκτυο των ~715 διαδρομών, τα 18/s δίνουν κύκλο ~40 s — ίδια
    τάξη με τη συχνότητα ανανέωσης του ΟΑΣΑ (~35 s), άρα χάνονται ελάχιστα
    στίγματα ενώ ο ρυθμός μένει μέσα στην καθαρή ζώνη (<1% 403 έως ~20/s).
    """
    conn = db.get_connection()
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass

    if route_codes is None:
        route_codes = [r["route_code"] for r in
                       conn.execute("SELECT route_code FROM routes ORDER BY route_code")]
    geoms = load_geometries(conn, route_codes)
    active = [rc for rc in route_codes if rc in geoms]
    if not active:
        log.error("Καμία διαδρομή με γεωμετρία — τρέξε πρώτα sync_shapes.py")
        return {}

    bounds = route_bounds(conn)
    tracker = GpsTracker(geoms)
    lim = limiter or oasa._SimpleLimiter(rate)
    cycle_s = len(active) / rate
    log.info("GPS: %d διαδρομές στα %.0f req/s → κύκλος ~%.0fs",
             len(active), rate, cycle_s)

    started = time.time()
    i = 0
    written = 0
    errors = {"total": 0, "forbidden": 0, "empty": 0}
    last_commit = time.time()
    last_log = time.time()

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if duration_s is not None and time.time() - started >= duration_s:
                break

            rc = active[i % len(active)]
            i += 1
            lim.acquire()
            # ΜΕΤΡΑΜΕ τα σφάλματα αντί να τα καταπίνουμε. Μαθημένο ακριβά: ο
            # poller του getStopArrivals τυλίγει τα πάντα σε `except Exception:
            # pass`, οπότε το ποσοστό 403 του ήταν ΑΟΡΑΤΟ — και μια «διόρθωση»
            # που βασίστηκε στα συμπτώματα των logs αντί σε μέτρηση έκανε 66
            # γραμμές να αποτύχουν. Ένας μετρητής που δεν κοιτάς είναι το ίδιο
            # με μετρητή που δεν υπάρχει.
            try:
                fixes = oasa.get_bus_location(rc)
            except oasa.OasaApiError as e:
                errors["total"] += 1
                if "rate-limited" in str(e):
                    errors["forbidden"] += 1
                continue
            except Exception:
                errors["total"] += 1
                continue
            if not fixes:
                errors["empty"] += 1
                continue

            new = tracker.ingest(rc, fixes)
            if new:
                written += write_passages(conn, new, bounds, db.now_utc_iso())

            if store_pings:
                polled = db.now_utc_iso()
                for f in fixes:
                    ts = _parse_cs_date(f.get("CS_DATE") or "")
                    if ts is None:
                        continue
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO vehicle_pings "
                            "(route_code, vehicle_no, lat, lng, ts_utc, "
                            " polled_at, heading) VALUES (?,?,?,?,?,?,?)",
                            (rc, str(f.get("VEH_NO")), float(f["CS_LAT"]),
                             float(f["CS_LNG"]), ts.isoformat(), polled,
                             int(f.get("VEH_HEADING") or 0)))
                    except Exception:
                        pass

            if time.time() - last_commit > 2.0:
                conn.commit()
                last_commit = time.time()
            if time.time() - last_log > 60.0:
                s = tracker.stats
                pct403 = (100.0 * errors["forbidden"] / i) if i else 0.0
                log.info("GPS: κλήσεις=%d στίγματα=%d (διπλά=%d) διελεύσεις=%d "
                         "γραμμένες=%d | 403=%d (%.2f%%) σφάλματα=%d κενές=%d "
                         "| βόλτες=%d εκτός_σχήματος=%d κενά=%d οχήματα=%d",
                         i, s["fixes"], s["dup"], s["passages"], written,
                         errors["forbidden"], pct403, errors["total"],
                         errors["empty"], s["laps"], s["unprojectable"],
                         s["gap_skips"], len(tracker.state))
                last_log = time.time()
    finally:
        try:
            conn.commit()
            conn.close()
        except Exception:
            pass

    return {**tracker.stats, "written": written, "calls": i,
            "http_403": errors["forbidden"], "api_errors": errors["total"],
            "empty_routes": errors["empty"],
            "pct_403": round(100.0 * errors["forbidden"] / i, 3) if i else 0.0,
            "elapsed_s": round(time.time() - started, 1)}


def main():
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="GPS passage tracker")
    ap.add_argument("--rate", type=float, default=18.0)
    ap.add_argument("--minutes", type=float, default=None,
                    help="τρέξε τόση ώρα και σταμάτα (αλλιώς: για πάντα)")
    ap.add_argument("--routes", default=None, help="λίστα route_code χωρισμένη με κόμμα")
    ap.add_argument("--store-pings", action="store_true",
                    help="κράτα και τα ακατέργαστα στίγματα (μόνο για έλεγχο)")
    args = ap.parse_args()

    db.ensure_schema()
    rcs = args.routes.split(",") if args.routes else None
    stats = run(rate=args.rate,
                duration_s=args.minutes * 60 if args.minutes else None,
                route_codes=rcs, store_pings=args.store_pings)
    log.info("Τέλος: %s", stats)


if __name__ == "__main__":
    main()
