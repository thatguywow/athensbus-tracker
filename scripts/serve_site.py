"""
serve_site.py — self-hosted mode (χωρίς GitHub).

Σερβίρει το dashboard (φάκελος docs/) από τοπικό web server ΚΑΙ τρέχει σε
βρόχο τον κύκλο δεδομένων: sync προγράμματος → compute → generate. Η σελίδα
ενημερώνεται ΑΜΕΣΩΣ μετά από κάθε compute (τα JSON ξαναγράφονται επί τόπου
και σερβίρονται χωρίς cache) — κανένα git push, καμία εξάρτηση από GitHub.

Ο poller (scripts/local_poller.py) τρέχει ΞΕΧΩΡΙΣΤΑ, όπως πάντα.

Χρήση:
    python scripts/serve_site.py [--port 8000] [--interval 15]

  --port      θύρα του web server (προεπιλογή 8000, ή env PORT)
  --interval  λεπτά ανάμεσα στους κύκλους δεδομένων (προεπιλογή 15,
              ή env CYCLE_MINUTES). Το ημερήσιο πρόγραμμα συγχρονίζεται
              το πολύ μία φορά την ώρα ό,τι interval κι αν βάλεις· το
              εβδομαδιαίο μία φορά τη μέρα — ίδια λογική με το run_hourly.

Η υπάρχουσα λειτουργία (run_hourly + GitHub Pages) μένει εντελώς ανέγγιχτη.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from datetime import date, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(REPO_ROOT / "serve_site.log"), encoding="utf-8"),
    ],
    force=True,   # imported modules configure logging first; override them
)
log = logging.getLogger("serve_site")


# ── web server ───────────────────────────────────────────────────────────────
class NoCacheHandler(SimpleHTTPRequestHandler):
    """Static files from docs/ with no-cache headers, so every reload of the
    page sees the freshest JSON right after a compute."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass   # keep the console clean; access logs add nothing here


def start_web_server(port: int) -> ThreadingHTTPServer:
    handler = partial(NoCacheHandler, directory=str(DOCS_DIR))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    log.info("Web server: http://0.0.0.0:%d  (φάκελος: %s)", port, DOCS_DIR)
    return httpd


# ── data cycle ───────────────────────────────────────────────────────────────
_last_daily_sync_hour: str | None = None   # "YYYY-MM-DDTHH" of last daily sync


def _schedule_synced_today(conn) -> bool:
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) c FROM scheduled_trips WHERE schedule_date=?", (today,)
    ).fetchone()
    return (row["c"] or 0) > 0


def run_cycle():
    """One data cycle: (hourly-capped) schedule sync → compute → generate."""
    global _last_daily_sync_hour

    import sync_schedules
    import compute_daily_report
    import generate_site_data

    # 1) schedule sync — at most once per hour; normal (weekly) schedule only
    #    on the first sync of the day. Ίδια λογική με το run_hourly.
    hour_key = datetime.now().strftime("%Y-%m-%dT%H")
    if _last_daily_sync_hour != hour_key:
        conn = db.get_connection()
        first_of_day = not _schedule_synced_today(conn)
        conn.close()
        log.info("Sync προγράμματος (%s)...",
                 "πλήρης, πρώτος της μέρας" if first_of_day else "ωριαίο refresh")
        try:
            sync_schedules.main(include_normal=first_of_day)
            _last_daily_sync_hour = hour_key
        except Exception as e:
            log.warning("Sync απέτυχε (μη μοιραίο): %s", e)

    # 2) compute (με το handover window της χθεσινής, όπως πάντα)
    log.info("Compute...")
    try:
        compute_daily_report.main()
    except Exception as e:
        log.error("Compute απέτυχε: %s", e)
        return

    # 3) generate — η σελίδα ενημερώνεται στη στιγμή (no-cache serving)
    log.info("Generate site data...")
    try:
        generate_site_data.main()
        log.info("Η σελίδα ενημερώθηκε.")
    except Exception as e:
        log.error("Generate απέτυχε: %s", e)


def main():
    ap = argparse.ArgumentParser(description="Self-hosted dashboard server")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--interval", type=float,
                    default=float(os.environ.get("CYCLE_MINUTES", "15")),
                    help="λεπτά μεταξύ κύκλων δεδομένων")
    args = ap.parse_args()

    db.ensure_schema()
    start_web_server(args.port)
    log.info("Κύκλος δεδομένων κάθε %.0f λεπτά. Ctrl+C για τερματισμό.",
             args.interval)

    while True:
        started = time.time()
        try:
            run_cycle()
        except Exception as e:   # ο βρόχος δεν πεθαίνει ποτέ από ένα σφάλμα
            log.error("Σφάλμα κύκλου: %s", e)
        elapsed = time.time() - started
        wait = max(30.0, args.interval * 60 - elapsed)
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Τερματισμός.")
