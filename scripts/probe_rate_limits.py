"""
probe_rate_limits.py — πού ακριβώς αρχίζει ο ΟΑΣΑ να απαντά 403;

Γιατί υπάρχει: ο poller εγκατέλειψε το getBusLocation επειδή «έβγαζε 403».
Όμως το GPS μονοπάτι ΔΕΝ καλούνταν ποτέ με την ίδια πειθαρχία που έχει σήμερα
το getStopArrivals:

    get_stop_arrivals   → attempts=1, retry_forbidden=False, timeout=5
                          + RateLimiter (token bucket) στον _stop_worker
    get_bus_location    → attempts=4 (MAX_RETRIES), retry_forbidden=True,
                          timeout=20, ΚΑΜΙΑ ρύθμιση ρυθμού στο
                          batch_get_bus_locations (σκέτο ThreadPoolExecutor)

Δηλαδή σε ένα 403 το παλιό GPS μονοπάτι ξαναχτυπούσε 4 φορές με backoff, ενώ
έτρεχε ήδη χωρίς όριο ρυθμού — ενίσχυση, όχι υποχώρηση. Αυτό το probe απαντά
αν το endpoint είναι όντως πιο αυστηρό, ή αν έφταιγε ο τρόπος κλήσης.

ΕΛΕΓΧΟΜΕΝΟ ΠΕΙΡΑΜΑ: ίδιος host, ίδιο δευτερόλεπτο, ίδιος ρυθμός, δύο endpoints.
Μία μεταβλητή αλλάζει — ποιο endpoint. Χωρίς retries πουθενά, ώστε να μετράμε
την απάντηση του server και όχι τη δική μας επιμονή.

Τρέξε ΚΑΙ στον VPS: το όριο είναι ανά IP, οπότε το αποτέλεσμα από άλλο δίκτυο
δεν μεταφέρεται αυτούσιο.

    python scripts/probe_rate_limits.py            # πλήρες, ~4 λεπτά
    python scripts/probe_rate_limits.py --quick    # ~1 λεπτό
"""

from __future__ import annotations

import argparse
import json
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

BASE = "https://telematics.oasa.gr/api/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_CTX = ssl.create_default_context()

# Η ελληνική κονσόλα των Windows είναι cp1253 και πνίγεται στα box-drawing
# χαρακτήρες του πίνακα. Το output είναι διαγνωστικό — δεν αξίζει να σκάει.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Φάσεις: (req/s, διάρκεια σε δευτερόλεπτα). Ανεβαίνουμε σταδιακά και
# σταματάμε νωρίς αν τα 403 ξεφύγουν — δεν χρειάζεται να πονέσουμε τον server
# για να μάθουμε ότι πονάει.
PHASES = [(10, 30), (20, 30), (30, 30), (45, 30)]
QUICK_PHASES = [(10, 15), (25, 15), (40, 15)]
ABORT_403_PCT = 15.0     # πάνω από αυτό, η φάση κόβεται


def _get(act: str, p1: str, timeout: float = 8.0) -> tuple[str, float]:
    """Μία κλήση, ΜΗΔΕΝ retries. Επιστρέφει (αποτέλεσμα, latency)."""
    url = f"{BASE}?act={act}&p1={p1}"
    req = urllib.request.Request(url, headers=UA)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            r.read()
            return f"http_{r.status}", time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return f"http_{e.code}", time.monotonic() - t0
    except (urllib.error.URLError, TimeoutError, OSError):
        return "network_error", time.monotonic() - t0
    except Exception:
        return "other_error", time.monotonic() - t0


class TokenBucket:
    """Ίδια λογική με τον RateLimiter του local_poller (BURST_CAP=5)."""

    def __init__(self, rate: float, burst_cap: float = 5.0):
        self.rate = float(rate)
        # βλ. oasa_client._SimpleLimiter: cap < 1 ⇒ η acquire() δεν επιστρέφει ποτέ
        self.cap = max(1.0, min(self.rate, burst_cap))
        self.allowance = self.cap
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.allowance += (now - self.last) * self.rate
                self.last = now
                self.allowance = min(self.allowance, self.cap)
                if self.allowance >= 1.0:
                    self.allowance -= 1.0
                    return
            time.sleep(0.005)


def run_phase(act: str, targets: list[str], rate: float, duration: float,
              workers: int = 24) -> dict:
    """Σταθερός ρυθμός `rate` για `duration` δευτερόλεπτα. Χωρίς retries."""
    bucket = TokenBucket(rate)
    counts: Counter = Counter()
    lats: list[float] = []
    lock = threading.Lock()
    deadline = time.monotonic() + duration
    aborted = False
    idx = [0]

    def worker():
        nonlocal aborted
        while time.monotonic() < deadline and not aborted:
            bucket.acquire()
            if time.monotonic() >= deadline or aborted:
                return
            with lock:
                target = targets[idx[0] % len(targets)]
                idx[0] += 1
            outcome, lat = _get(act, target)
            with lock:
                counts[outcome] += 1
                lats.append(lat)
                total = sum(counts.values())
                # Νωρίς-έξοδος: αρκετό δείγμα ΚΑΙ καθαρά πάνω από το κατώφλι
                if total >= 40:
                    pct = 100.0 * counts["http_403"] / total
                    if pct > ABORT_403_PCT:
                        aborted = True

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in range(workers):
            pool.submit(worker)
    elapsed = time.monotonic() - started

    total = sum(counts.values()) or 1
    lats.sort()
    return {
        "act": act,
        "target_rate": rate,
        "actual_rate": round(sum(counts.values()) / max(elapsed, 0.001), 1),
        "total": sum(counts.values()),
        "ok": counts["http_200"],
        "forbidden": counts["http_403"],
        "ratelimited": counts["http_429"],
        "notfound": counts["http_404"],
        "neterr": counts["network_error"],
        "pct_403": round(100.0 * counts["http_403"] / total, 2),
        "pct_ok": round(100.0 * counts["http_200"] / total, 2),
        "p50_ms": round(lats[len(lats) // 2] * 1000) if lats else None,
        "p95_ms": round(lats[int(len(lats) * 0.95)] * 1000) if lats else None,
        "aborted": aborted,
        "raw": dict(counts),
    }


def load_targets(n_routes: int = 120) -> tuple[list[str], list[str]]:
    """Μαζεύει route_codes και stop_codes για τα δύο σκέλη του πειράματος."""
    print("Φόρτωση στόχων…", flush=True)
    try:
        req = urllib.request.Request(f"{BASE}?act=webGetLines", headers=UA)
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            lines = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"  ΣΦΑΛΜΑ φόρτωσης γραμμών: {e}")
        sys.exit(1)

    random.seed(42)                       # ίδιοι στόχοι σε κάθε εκτέλεση
    random.shuffle(lines)
    routes: list[str] = []
    for ln in lines:
        code = ln.get("LineCode") or ln.get("line_code")
        if not code:
            continue
        try:
            req = urllib.request.Request(
                f"{BASE}?act=webGetRoutes&p1={code}", headers=UA)
            with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
                for rt in json.loads(r.read().decode("utf-8", "replace")):
                    if rt.get("RouteCode"):
                        routes.append(str(rt["RouteCode"]))
        except Exception:
            continue
        if len(routes) >= n_routes:
            break
        time.sleep(0.05)

    stops: list[str] = []
    for rc in routes[:25]:
        try:
            req = urllib.request.Request(
                f"{BASE}?act=webGetStops&p1={rc}", headers=UA)
            with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
                for st in json.loads(r.read().decode("utf-8", "replace")):
                    sid = st.get("StopCode") or st.get("StopID")
                    if sid:
                        stops.append(str(sid))
        except Exception:
            continue
        time.sleep(0.05)

    stops = list(dict.fromkeys(stops))
    print(f"  {len(routes)} routes, {len(stops)} stops\n")
    if not routes or not stops:
        print("Δεν βρέθηκαν αρκετοί στόχοι — άκυρο πείραμα.")
        sys.exit(1)
    return routes, stops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="σύντομη εκδοχή")
    ap.add_argument("--out", default="rate_probe_results.json")
    args = ap.parse_args()

    phases = QUICK_PHASES if args.quick else PHASES
    routes, stops = load_targets()

    print(f"{'endpoint':<20} {'ρυθμός':>7} {'πραγμ.':>7} {'κλήσεις':>8} "
          f"{'200':>6} {'403':>6} {'429':>5} {'net':>5} {'%403':>7} "
          f"{'p50ms':>6} {'p95ms':>6}")
    print("─" * 96)

    results = []
    # Εναλλάξ τα δύο endpoints ΑΝΑ ΦΑΣΗ: αν ο server έχει «μνήμη» (sliding
    # window), θέλουμε και τα δύο να τη συναντήσουν στις ίδιες συνθήκες.
    for rate, dur in phases:
        for act, targets in (("getStopArrivals", stops),
                             ("getBusLocation", routes)):
            res = run_phase(act, targets, rate, dur)
            results.append(res)
            flag = "  ⚠ ΔΙΑΚΟΠΗ" if res["aborted"] else ""
            print(f"{act:<20} {rate:>7} {res['actual_rate']:>7} "
                  f"{res['total']:>8} {res['ok']:>6} {res['forbidden']:>6} "
                  f"{res['ratelimited']:>5} {res['neterr']:>5} "
                  f"{res['pct_403']:>6}% {str(res['p50_ms']):>6} "
                  f"{str(res['p95_ms']):>6}{flag}", flush=True)
            time.sleep(5)      # ανάσα ανάμεσα στις φάσεις

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "results": results}, f, ensure_ascii=False, indent=2)

    print("\n── Συμπέρασμα ─────────────────────────────────────────────")
    gbl = [r for r in results if r["act"] == "getBusLocation"]
    gsa = [r for r in results if r["act"] == "getStopArrivals"]
    worst_gbl = max((r["pct_403"] for r in gbl), default=0)
    worst_gsa = max((r["pct_403"] for r in gsa), default=0)
    print(f"χειρότερο %403 — getBusLocation: {worst_gbl}%   "
          f"getStopArrivals: {worst_gsa}%")
    safe = [r["target_rate"] for r in gbl if r["pct_403"] < 1.0]
    if safe:
        print(f"getBusLocation καθαρό (<1% 403) έως {max(safe)} req/s")
    if worst_gbl <= worst_gsa + 2:
        print("→ Τα δύο endpoints συμπεριφέρονται ΙΔΙΑ. Τα παλιά 403 "
              "οφείλονταν στον τρόπο κλήσης (χωρίς rate limit + 4 retries), "
              "όχι στο endpoint.")
    else:
        print("→ Το getBusLocation ΕΙΝΑΙ αυστηρότερο. Χρειάζεται χαμηλότερος "
              "ρυθμός/μεγαλύτερο διάστημα ανανέωσης.")
    print(f"\nΑποτελέσματα: {args.out}")


if __name__ == "__main__":
    main()
