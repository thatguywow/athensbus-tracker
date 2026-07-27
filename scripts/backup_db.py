"""
backup_db.py — ασφαλές αντίγραφο της βάσης, με περιστροφή.

Η βάση είναι το μοναδικό μη-αναπαραγώγιμο περιουσιακό στοιχείο του project:
ο κώδικας ξαναγράφεται, τα ωμά δεδομένα 30 ημερών ΟΧΙ. Ένας δίσκος που
χαλάει, ένας πάροχος που κλείνει ή ένα λάθος `rm` τα σβήνει οριστικά.

Τι κάνει:
  • Χρησιμοποιεί το sqlite3 `.backup` (online API): συνεπές στιγμιότυπο ΧΩΡΙΣ
    να σταματήσει ο poller — δεν είναι σκέτο cp που μπορεί να πιάσει τη βάση
    στη μέση εγγραφής.
  • Συμπιέζει (gzip) — η βάση συμπιέζεται τυπικά 5-10×.
  • Κρατά τα τελευταία KEEP αρχεία και σβήνει τα παλιότερα.
  • Γράφει checksum ώστε να μπορείς να επιβεβαιώσεις μεταφορά.

Χρήση:
    python3 scripts/backup_db.py                 # → backups/
    python3 scripts/backup_db.py /mnt/ext        # σε άλλη διαδρομή

Cron (καθημερινά 04:40, μετά το handover window):
    40 4 * * * cd /opt/athensbus-tracker && python3 scripts/backup_db.py \\
               >> backup.log 2>&1

ΣΗΜΑΝΤΙΚΟ: αντίγραφο ΣΤΟΝ ΙΔΙΟ δίσκο δεν είναι backup — προστατεύει από λάθος
διαγραφή, όχι από απώλεια μηχανήματος. Κατέβασέ τα περιοδικά, π.χ. από το PC:
    scp root@<IP>:/opt/athensbus-tracker/backups/*.gz D:\\athensbus-backups\\
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import db  # noqa: E402

KEEP = 7            # πόσα αντίγραφα κρατάμε
CHUNK = 1 << 20


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()[:16]


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "backups")
    os.makedirs(out_dir, exist_ok=True)

    src = db.DB_PATH if hasattr(db, "DB_PATH") else os.path.join(
        os.path.dirname(__file__), "..", "db", "athensbus.db")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tmp = os.path.join(out_dir, f"athensbus-{stamp}.db")
    final = tmp + ".gz"

    t0 = time.time()
    # Online backup: consistent snapshot while the poller keeps writing.
    source = sqlite3.connect(src)
    dest = sqlite3.connect(tmp)
    with dest:
        source.backup(dest)
    dest.close()
    source.close()

    with open(tmp, "rb") as fi, gzip.open(final, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, CHUNK)
    raw = os.path.getsize(tmp)
    os.remove(tmp)
    comp = os.path.getsize(final)

    print(f"{os.path.basename(final)}  {raw/1e6:.0f}MB → {comp/1e6:.0f}MB "
          f"({raw/max(1,comp):.1f}× συμπίεση, {time.time()-t0:.0f}s) "
          f"sha256:{_sha256(final)}")

    # Rotation: keep the newest KEEP files.
    files = sorted(f for f in os.listdir(out_dir)
                   if f.startswith("athensbus-") and f.endswith(".db.gz"))
    for old in files[:-KEEP]:
        os.remove(os.path.join(out_dir, old))
        print(f"  διαγράφηκε παλιό: {old}")
    print(f"  αντίγραφα: {min(len(files), KEEP)}/{KEEP} | φάκελος: {out_dir}")


if __name__ == "__main__":
    main()
