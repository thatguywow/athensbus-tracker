"""
_cpu.py — προσωρινό διαγνωστικό: πού πάει το CPU του poller.

Απαντά σε τρία: (Α) κοστίζει λιγότερο μια επαναχρησιμοποιημένη σύνδεση;
(Β) τιμά ο ΟΑΣΑ το keep-alive; (Γ) ποια συνάρτηση καίει το CPU;

Read-only, ~70 κλήσεις (~1,5/s επιπλέον — αμελητέο μπροστά στα 25/s).
Τρέξε:  python3 _cpu.py
Μετά:   git rm _cpu.py   (δεν ανήκει στο repo μόνιμα)
"""

import sys, time
sys.path.insert(0, "scripts")
import requests
import oasa_client as oasa

STOP = "10399"
url = oasa.BASE_URL
prm = {"act": "getStopArrivals", "p1": STOP}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def bench(label, get):
    try:
        get()                                   # warm-up
    except Exception as e:
        print(f"{label}: ΣΦΑΛΜΑ {e}")
        return
    c0, w0 = time.process_time(), time.time()
    n = 0
    for _ in range(20):
        try:
            get()
            n += 1
        except Exception:
            pass
    c, w = time.process_time() - c0, time.time() - w0
    if n:
        print(f"{label:<28} CPU={c*1000/n:6.1f} ms/κλήση   "
              f"wall={w*1000/n:6.0f} ms/κλήση  (ok {n}/20)")


print("═══ Α/Β) κόστος ανά κλήση ═══")
bench("νέα σύνδεση κάθε κλήση",
      lambda: requests.get(url, params=prm, headers=UA, timeout=15))

s = requests.Session()
s.headers.update(UA)
bench("επαναχρησιμοποίηση συνόδου", lambda: s.get(url, params=prm, timeout=15))

print("\n═══ Γ) τι λέει ο server για keep-alive ═══")
try:
    r = s.get(url, params=prm, timeout=15)
    print("headers:", {k: v for k, v in r.headers.items()
                       if k.lower() in ("connection", "keep-alive",
                                        "server", "content-type")})
    print("HTTP version:", r.raw.version, "(11 = HTTP/1.1)")
except Exception as e:
    print("ΣΦΑΛΜΑ:", e)

print("\n═══ Δ) profile: πού καίγεται το CPU (tottime) ═══")
import cProfile, pstats, io
pr = cProfile.Profile()
pr.enable()
for _ in range(30):
    try:
        oasa._request("getStopArrivals", {"p1": STOP}, attempts=1)
    except Exception:
        pass
pr.disable()
buf = io.StringIO()
pstats.Stats(pr, stream=buf).sort_stats("tottime").print_stats(14)
print("\n".join(buf.getvalue().splitlines()[4:22]))
