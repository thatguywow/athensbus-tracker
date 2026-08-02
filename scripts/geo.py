"""
geo.py — προβολή στίγματος GPS πάνω στη γεωμετρία της διαδρομής.

ΓΙΑΤΙ ΥΠΑΡΧΕΙ
=============
Ένα στίγμα (lat, lng) από μόνο του δεν λέει «πού είναι το λεωφορείο στη
διαδρομή». Το ερώτημα που μας ενδιαφέρει είναι μονοδιάστατο: **πόσα μέτρα έχει
διανύσει**. Μόλις κάθε στίγμα γίνει ένας αριθμός σε αυτόν τον άξονα — και κάθε
στάση επίσης ένας αριθμός στον ίδιο άξονα — η ώρα διέλευσης από στάση παύει να
είναι εικασία: είναι γραμμική παρεμβολή ανάμεσα στα δύο στίγματα που την
περικλείουν.

Αυτό αντικαθιστά το «πόσο κοντά είναι το λεωφορείο σε αυτή τη στάση;», που
σπάει σε κάθε διαδρομή που περνά δύο φορές από το ίδιο σημείο (κυκλικές,
επιστροφές σε μονόδρομους, τερματικοί βρόχοι).

ΓΙΑΤΙ ΟΧΙ ΕΥΘΕΙΕΣ ΜΕΤΑΞΥ ΣΤΑΣΕΩΝ
=================================
Η υπάρχουσα _stop_distances μετρά ευθεία γραμμή από στάση σε στάση. Σε ένα
δρομολόγιο με στροφές, μονόδρομους και παρακάμψεις αυτό υπο-μετρά συστηματικά
την απόσταση, και επειδή είναι ο άξονας x κάθε παλινδρόμησης, το σφάλμα
διαχέεται σε όλες τις εκτιμήσεις. Η πραγματική πολυγραμμή έρχεται δωρεάν από
το webGetRoutesDetailsAndStops.

ΑΚΡΙΒΕΙΑ ΣΥΝΤΕΤΑΓΜΕΝΩΝ
======================
Ισοαπέχουσα (equirectangular) προβολή με συντελεστές υπολογισμένους στο ΜΕΣΟ
ΠΛΑΤΟΣ της κάθε διαδρομής. Σε έκταση λίγων δεκάδων χιλιομέτρων το σφάλμα είναι
κάτω από 0,1% — αμελητέο μπροστά στα ~5 m ακρίβειας του ίδιου του GPS, και
πολύ φθηνότερο από haversine σε κάθε τμήμα (τρέχει εκατομμύρια φορές τη μέρα
σε αδύναμο VPS).
"""

from __future__ import annotations

import math
from bisect import bisect_left

# Πόσο μακριά από την πολυγραμμή επιτρέπεται να πέσει ένα στίγμα για να
# θεωρηθεί «πάνω στη διαδρομή». Το αστικό GPS σε χαράδρα κτιρίων ξεφεύγει
# άνετα 20-30 m, και οι πολυγραμμές του ΟΑΣΑ είναι αραιές σε ευθείες.
MAX_SNAP_M = 120.0

# Παράθυρο αναζήτησης γύρω από την προηγούμενη θέση. Με ανανέωση στίγματος
# ~35 s, ακόμη και στα 80 km/h το όχημα διανύει ~780 m — το 2.500 m δίνει
# άφθονο περιθώριο, ενώ κόβει την αναζήτηση από ~500 τμήματα σε ~25.
FORWARD_WINDOW_M = 2500.0
BACKWARD_WINDOW_M = 300.0     # λίγο πίσω: θόρυβος GPS, ουρά σε φανάρι


def latlng_scale(lat_deg: float) -> tuple[float, float]:
    """
    Μέτρα ανά μοίρα (πλάτος, μήκος) στο δοσμένο γεωγραφικό πλάτος.

    Οι σταθερές 111000/88000 που χρησιμοποιεί σήμερα η _stop_distances είναι
    κοντά (~0,2% σφάλμα στην Αθήνα) αλλά σταθερές· εδώ υπολογίζονται σωστά,
    ώστε να μην κουβαλάμε συστηματική μεροληψία στον άξονα των αποστάσεων.
    """
    phi = math.radians(lat_deg)
    m_lat = (111132.92 - 559.82 * math.cos(2 * phi)
             + 1.175 * math.cos(4 * phi))
    m_lng = (111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi))
    return m_lat, max(m_lng, 1.0)


class RouteShape:
    """
    Η πολυγραμμή μιας διαδρομής, έτοιμη για προβολές.

    Τα σωρευτικά μήκη (`dist`) υπολογίζονται μία φορά· η project() τρέχει
    εκατομμύρια φορές τη μέρα και δεν πρέπει να ξαναπερνά την πολυγραμμή.
    """

    __slots__ = ("lat", "lng", "dist", "n", "m_lat", "m_lng", "total")

    def __init__(self, points: list[tuple[float, float]],
                 dists: list[float] | None = None):
        """points: [(lat, lng), ...] με τη σειρά διαδρομής."""
        if len(points) < 2:
            raise ValueError("η πολυγραμμή θέλει >=2 σημεία")
        self.lat = [float(p[0]) for p in points]
        self.lng = [float(p[1]) for p in points]
        self.n = len(points)
        mean_lat = sum(self.lat) / self.n
        self.m_lat, self.m_lng = latlng_scale(mean_lat)
        self.dist = list(dists) if dists else self._cumulative()
        self.total = self.dist[-1]

    def _cumulative(self) -> list[float]:
        out = [0.0]
        for i in range(1, self.n):
            dx = (self.lat[i] - self.lat[i - 1]) * self.m_lat
            dy = (self.lng[i] - self.lng[i - 1]) * self.m_lng
            out.append(out[-1] + math.hypot(dx, dy))
        return out

    # ── προβολή ────────────────────────────────────────────────────────────
    def project(self, lat: float, lng: float,
                near_dist: float | None = None) -> tuple[float, float] | None:
        """
        Επιστρέφει (μέτρα_διανυθέντα, σφάλμα_κάθετης_απόστασης) ή None.

        near_dist: η προηγούμενη γνωστή θέση του ΙΔΙΟΥ οχήματος. Όταν δίνεται,
        η αναζήτηση περιορίζεται σε ένα παράθυρο γύρω της. Αυτό δεν είναι μόνο
        ταχύτητα — είναι ΟΡΘΟΤΗΤΑ: σε κυκλική διαδρομή ή σε επιστροφή από τον
        ίδιο δρόμο, η ίδια συντεταγμένη αντιστοιχεί σε ΔΥΟ θέσεις της
        πολυγραμμής, και μόνο η συνέχεια της κίνησης ξεχωρίζει ποια ισχύει.
        Χωρίς αυτό, ένα λεωφορείο στα μισά της επιστροφής «τηλεμεταφέρεται»
        στην αρχή της διαδρομής.
        """
        lo, hi = 0, self.n - 1
        if near_dist is not None:
            lo = max(0, self._seg_at(near_dist - BACKWARD_WINDOW_M) - 1)
            hi = min(self.n - 1, self._seg_at(near_dist + FORWARD_WINDOW_M) + 1)
            if hi - lo < 2:
                lo, hi = 0, self.n - 1

        best = self._scan(lat, lng, lo, hi)
        # Το παράθυρο μπορεί να αστόχησε (κενό στίγματος, εκτροπή, όχημα που
        # μπήκε στη διαδρομή αλλού). Δεύτερη ευκαιρία σε όλη την πολυγραμμή.
        if best is None or best[1] > MAX_SNAP_M:
            if (lo, hi) != (0, self.n - 1):
                full = self._scan(lat, lng, 0, self.n - 1)
                if full and (best is None or full[1] < best[1]):
                    best = full
        if best is None or best[1] > MAX_SNAP_M:
            return None
        return best

    def _scan(self, lat: float, lng: float, lo: int, hi: int):
        px = lat * self.m_lat
        py = lng * self.m_lng
        best_d = None
        best_err = float("inf")
        for i in range(lo, hi):
            ax = self.lat[i] * self.m_lat
            ay = self.lng[i] * self.m_lng
            bx = self.lat[i + 1] * self.m_lat
            by = self.lng[i + 1] * self.m_lng
            vx, vy = bx - ax, by - ay
            seg_len2 = vx * vx + vy * vy
            if seg_len2 <= 1e-9:
                continue
            # t = προβολή του σημείου στο ευθύγραμμο τμήμα, κομμένη στα [0,1]
            t = ((px - ax) * vx + (py - ay) * vy) / seg_len2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            cx, cy = ax + t * vx, ay + t * vy
            err = math.hypot(px - cx, py - cy)
            if err < best_err:
                best_err = err
                best_d = self.dist[i] + t * math.sqrt(seg_len2)
        return None if best_d is None else (best_d, best_err)

    def bearing_at(self, dist_m: float) -> float | None:
        """
        Πορεία της διαδρομής (μοίρες, 0=Βορράς) στο δοσμένο σωρευτικό μήκος.

        Συγκρίνεται με το VEH_HEADING του getBusLocation: αν το όχημα κοιτά
        αντίθετα από τη φορά της διαδρομής, η προβολή είναι λάθος.
        """
        i = self._seg_at(dist_m)
        j = min(i + 1, self.n - 1)
        if i == j:
            return None
        dx = (self.lat[j] - self.lat[i]) * self.m_lat      # βόρεια συνιστώσα
        dy = (self.lng[j] - self.lng[i]) * self.m_lng      # ανατολική
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return None
        return math.degrees(math.atan2(dy, dx)) % 360.0

    def _seg_at(self, dist_m: float) -> int:
        """Δείκτης τμήματος που περιέχει το δοσμένο σωρευτικό μήκος."""
        if dist_m <= 0:
            return 0
        if dist_m >= self.total:
            return self.n - 1
        return max(0, bisect_left(self.dist, dist_m) - 1)


def snap_stops(shape: RouteShape,
               stops: list[tuple[int, str, float, float]]
               ) -> list[tuple[int, str, float, float]]:
    """
    Τοποθετεί τις στάσεις πάνω στην πολυγραμμή.

    stops: [(stop_order, stop_code, lat, lng), ...] με τη σειρά διαδρομής.
    Επιστρέφει: [(stop_order, stop_code, dist_m, snap_err_m), ...]

    ΜΟΝΟΤΟΝΙΑ: οι στάσεις πρέπει να προκύψουν με ΑΥΞΟΥΣΑ απόσταση — η στάση 12
    δεν γίνεται να είναι πριν από την 11. Σε κυκλική διαδρομή όμως η στάση 1 και
    η στάση N είναι το ίδιο σημείο, οπότε μια ανεξάρτητη προβολή ανά στάση θα
    έβαζε την τελευταία στο μηδέν. Προβάλλουμε λοιπόν διαδοχικά, με κάθε στάση
    να ψάχνει ΜΠΡΟΣΤΑ από την προηγούμενη.
    """
    out = []
    cursor = 0.0
    for order, code, lat, lng in stops:
        res = shape.project(lat, lng, near_dist=cursor)
        if res is None:
            # Εκτός πολυγραμμής: κρατάμε τη θέση του δρομέα και σημαδεύουμε το
            # σφάλμα ως άπειρο, ώστε ο καλών να αποφασίσει αν εμπιστεύεται τη
            # διαδρομή. Δεν πετάμε τη στάση — θα έσπαγε η αρίθμηση.
            out.append((order, code, cursor, float("inf")))
            continue
        d, err = res
        if d < cursor:
            d = cursor          # δεν πάμε ποτέ πίσω
        out.append((order, code, d, err))
        cursor = d
    return out


def interpolate_crossing(d0: float, t0: float, d1: float, t1: float,
                         d_target: float) -> float:
    """
    Ώρα (epoch secs) που το όχημα πέρασε το d_target, ανάμεσα σε δύο στίγματα.

    Υποθέτει σταθερή ταχύτητα στο διάστημα. Με ~35 s ανάμεσα στα στίγματα αυτό
    είναι μια πολύ καλή προσέγγιση: το σφάλμα από επιτάχυνση/στάση σε φανάρι
    είναι λίγα δευτερόλεπτα, έναντι των ±30-60 s της ανίχνευσης εξαφάνισης.
    """
    if d1 <= d0:
        return t0
    frac = (d_target - d0) / (d1 - d0)
    frac = 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)
    return t0 + (t1 - t0) * frac
