"""
vacuum_db.py — συντήρηση χώρου (προαιρετική, για μηχανήματα με μικρό δίσκο).

Το SQLite δεν επιστρέφει χώρο στο λειτουργικό μετά τα καθαρίσματα 30 ημερών —
το αρχείο μένει στο μέγιστο μέγεθός του. Το VACUUM το συρρικνώνει.

ΠΡΟΣΟΧΗ: τρέξε το με ΣΤΑΜΑΤΗΜΕΝΟ poller/server (κλειδώνει τη βάση για
όσο διαρκεί — συνήθως λίγα λεπτά) και με ελεύθερο χώρο ≥ το μέγεθος της
βάσης (το VACUUM γράφει προσωρινό αντίγραφο).

Χρήση:  python scripts/vacuum_db.py
Cron (Linux, π.χ. 1η κάθε μήνα 04:30 — αφού σταματήσεις τα services):
  30 4 1 * *  systemctl stop athensbus-poller athensbus-server && \
              cd /opt/athensbus-tracker && python3 scripts/vacuum_db.py && \
              systemctl start athensbus-poller athensbus-server
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(__file__))
import db  # noqa

path = db.DB_PATH if hasattr(db, "DB_PATH") else os.path.join(
    os.path.dirname(__file__), "..", "db", "athensbus.db")
before = os.path.getsize(path)
print(f"Μέγεθος πριν: {before/1e6:.1f} MB — VACUUM σε εξέλιξη (μην διακόψεις)…")
conn = sqlite3.connect(path)
conn.execute("VACUUM")
conn.close()
after = os.path.getsize(path)
print(f"Μέγεθος μετά: {after/1e6:.1f} MB (εξοικονόμηση {(before-after)/1e6:.1f} MB)")
