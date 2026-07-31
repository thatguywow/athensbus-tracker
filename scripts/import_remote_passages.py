"""
import_remote_passages.py — φέρνει τις διελεύσεις του VPS στην τοπική βάση.

ΓΙΑΤΙ
=====
Οι δύο μέθοδοι τρέχουν σε ΔΙΑΦΟΡΕΤΙΚΑ μηχανήματα, κι αυτό είναι σκόπιμο: το
όριο ρυθμού του ΟΑΣΑ είναι ανά IP, οπότε ο GPS poller στο σπίτι δεν κλέβει
budget από τον poller του VPS. Καμία από τις δύο δεν χειροτερεύει την άλλη —
αλλά για να συγκριθούν πρέπει να βρεθούν στην ΙΔΙΑ βάση.

Αυτό το script κατεβάζει τις διελεύσεις μιας ημέρας βάρδιας από τον VPS
(ΜΟΝΟ ανάγνωση εκεί) και τις γράφει τοπικά. Το UNIQUE κλειδί του
stop_passages κάνει την εισαγωγή idempotent: ξανατρέξιμο δεν διπλογράφει.

ΧΡΗΣΗ
    python scripts/import_remote_passages.py 2026-08-01
    python scripts/import_remote_passages.py 2026-08-01 --host athensbus-vps

Προϋπόθεση: ρυθμισμένο ~/.ssh/config με το όνομα του host (χωρίς κωδικό).
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db

DEFAULT_HOST = "athensbus-vps"
DEFAULT_REMOTE_DB = "/opt/athensbus-tracker/db/athensbus.db"

# Τρέχει ΣΤΟΝ VPS. Ανοίγει τη βάση read-only (mode=ro) ώστε να μην μπορεί να
# πειράξει τίποτα ακόμη κι αν κάτι πάει στραβά, και βγάζει gzip στο stdout.
_REMOTE = r'''
import sqlite3, sys, gzip, json
c = sqlite3.connect("file:%s?mode=ro", uri=True)
out = gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb")
n = 0
for r in c.execute("""
        SELECT route_code, stop_code, stop_type, stop_order, vehicle_no,
               passed_at, service_date, recorded_at
        FROM stop_passages WHERE service_date = ?""", ("%s",)):
    out.write((json.dumps(r) + "\n").encode())
    n += 1
out.close()
sys.stderr.write("rows=%%d\n" %% n)
'''


def fetch(host: str, remote_db: str, service_date: str) -> list[list]:
    script = _REMOTE % (remote_db, service_date)
    print(f"Λήψη διελεύσεων {service_date} από {host}…", flush=True)
    # Το script πάει από το STDIN (`python3 -`), όχι ως -c: με το -c το ssh
    # ενώνει τα ορίσματα και ο ΑΠΟΜΑΚΡΥΣΜΕΝΟΣ φλοιός ξανα-αναλύει το κείμενο,
    # οπότε κάθε αλλαγή γραμμής γίνεται ξεχωριστή εντολή bash.
    proc = subprocess.run(
        ["ssh", host, "python3", "-"],
        input=script.encode("utf-8"), capture_output=True, timeout=1800)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[:500]
        raise SystemExit(f"Η λήψη απέτυχε:\n{err}")
    raw = gzip.decompress(proc.stdout) if proc.stdout else b""
    rows = [json.loads(ln) for ln in raw.decode("utf-8").splitlines() if ln.strip()]
    note = proc.stderr.decode("utf-8", "replace").strip()
    print(f"  {len(rows):,} σειρές ({len(proc.stdout)/1024:.0f} KB συμπιεσμένα) {note}")
    return rows


def store(rows: list[list]) -> dict:
    conn = db.get_connection()
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM stop_passages WHERE "
            "COALESCE(method,'disappearance')='disappearance'").fetchone()[0]
        for r in rows:
            # method='disappearance' ρητά: ό,τι έρχεται από τον VPS προέρχεται
            # από τον poller του getStopArrivals, και θέλουμε να ξεχωρίζει από
            # τις τοπικές διελεύσεις GPS στη σύγκριση.
            conn.execute("""
                INSERT OR IGNORE INTO stop_passages
                    (route_code, stop_code, stop_type, stop_order, vehicle_no,
                     passed_at, service_date, recorded_at, method)
                VALUES (?,?,?,?,?,?,?,?,'disappearance')""", r)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM stop_passages WHERE "
            "COALESCE(method,'disappearance')='disappearance'").fetchone()[0]
        return {"received": len(rows), "inserted": after - before,
                "already_present": len(rows) - (after - before)}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Import VPS passages locally")
    ap.add_argument("service_date", help="YYYY-MM-DD (ημέρα βάρδιας)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--remote-db", default=DEFAULT_REMOTE_DB)
    args = ap.parse_args()

    db.ensure_schema()
    rows = fetch(args.host, args.remote_db, args.service_date)
    if not rows:
        print("Καμία διέλευση για αυτή την ημέρα — τίποτα να εισαχθεί.")
        return
    res = store(rows)
    print(f"Εισήχθησαν {res['inserted']:,} νέες "
          f"({res['already_present']:,} υπήρχαν ήδη)")
    print(f"\nΤώρα: python scripts/compare_methods.py {args.service_date} --reconstruct")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    main()
