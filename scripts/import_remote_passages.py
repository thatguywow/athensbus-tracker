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
#
# ΔΕΝ φέρνει μόνο διελεύσεις. Η ανακατασκευή χρειάζεται και:
#   scheduled_trips  — αλλιώς δεν γίνεται καμία ανάθεση καρτελακιού
#   route_rotation   — η μαθημένη median_trip_duration_mins τροφοδοτεί ΚΑΘΕ
#                      εφεδρική εκτίμηση αναχώρησης/άφιξης
#   segment_times    — οι μαθημένοι χρόνοι τμημάτων origin→στάση
# Χωρίς αυτά η τοπική ανακατασκευή τρέχει ακρωτηριασμένη και η σύγκριση με τον
# VPS γίνεται άδικη: θα συγκρίναμε τη GPS χωρίς ιστορικό με την εξαφάνιση ΜΕ
# ιστορικό δεκάδων ημερών.
_REMOTE = r'''
import sqlite3, sys, gzip, json
c = sqlite3.connect("file:%s?mode=ro", uri=True)
sd = "%s"
out = gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb")
counts = {}

def dump(tag, sql, args=()):
    n = 0
    for r in c.execute(sql, args):
        out.write((json.dumps([tag] + list(r)) + "\n").encode())
        n += 1
    counts[tag] = n

dump("passage", """SELECT route_code, stop_code, stop_type, stop_order,
                          vehicle_no, passed_at, service_date, recorded_at
                   FROM stop_passages WHERE service_date = ?""", (sd,))
dump("sched", """SELECT route_code, schedule_date, departure_time,
                        raw_sdd_code, last_synced
                 FROM scheduled_trips WHERE schedule_date = ?""", (sd,))
dump("rotation", """SELECT route_code, slot_count, median_cycle_mins,
                           median_headway_mins, median_trip_duration_mins,
                           duration_samples, confidence_days, cycle_samples,
                           last_updated FROM route_rotation""")
dump("segment", """SELECT route_code, stop_order, median_mins, samples,
                          last_updated FROM segment_times""")
out.close()
sys.stderr.write(json.dumps(counts) + "\n")
'''

# Πού πάει κάθε γραμμή, ανά ετικέτα.
_TARGETS = {
    "passage": ("""INSERT OR IGNORE INTO stop_passages
                     (route_code, stop_code, stop_type, stop_order, vehicle_no,
                      passed_at, service_date, recorded_at, method)
                   VALUES (?,?,?,?,?,?,?,?,'disappearance')""", 8),
    "sched":   ("""INSERT OR IGNORE INTO scheduled_trips
                     (route_code, schedule_date, departure_time, raw_sdd_code,
                      last_synced) VALUES (?,?,?,?,?)""", 5),
    "rotation": ("""INSERT INTO route_rotation
                     (route_code, slot_count, median_cycle_mins,
                      median_headway_mins, median_trip_duration_mins,
                      duration_samples, confidence_days, cycle_samples,
                      last_updated) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(route_code) DO UPDATE SET
                      slot_count=excluded.slot_count,
                      median_cycle_mins=excluded.median_cycle_mins,
                      median_headway_mins=excluded.median_headway_mins,
                      median_trip_duration_mins=excluded.median_trip_duration_mins,
                      duration_samples=excluded.duration_samples,
                      confidence_days=excluded.confidence_days,
                      cycle_samples=excluded.cycle_samples,
                      last_updated=excluded.last_updated""", 9),
    "segment": ("""INSERT INTO segment_times
                     (route_code, stop_order, median_mins, samples, last_updated)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(route_code, stop_order) DO UPDATE SET
                     median_mins=excluded.median_mins,
                     samples=excluded.samples,
                     last_updated=excluded.last_updated""", 5),
}


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
    stats: dict[str, int] = {}
    try:
        for r in rows:
            tag, payload = r[0], r[1:]
            target = _TARGETS.get(tag)
            if target is None:
                continue
            sql, ncols = target
            if len(payload) != ncols:
                continue
            try:
                conn.execute(sql, payload)
                stats[tag] = stats.get(tag, 0) + 1
            except Exception:
                stats[tag + "_failed"] = stats.get(tag + "_failed", 0) + 1
        conn.commit()
        return stats
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
        print("Τίποτα δεν επιστράφηκε για αυτή την ημέρα.")
        return
    res = store(rows)
    label = {"passage": "διελεύσεις", "sched": "προγραμματισμένα",
             "rotation": "route_rotation", "segment": "segment_times"}
    print("Εισήχθησαν:")
    for k, v in sorted(res.items()):
        print(f"  {label.get(k, k):<18} {v:>8,}")
    print(f"\nΤώρα: python scripts/compare_methods.py {args.service_date} --reconstruct")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    main()
