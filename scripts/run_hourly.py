"""
run_hourly.py — runs once per hour via Windows Task Scheduler.

1. Syncs today's schedule from OASA (if not already synced today)
2. Reconstructs trips from today's pings
3. Computes rotation slots and vehicle activity
4. Generates static site JSON files
5. Commits db/athensbus.db + docs/data/ to GitHub and pushes

Usage:
    python scripts/run_hourly.py

Set up in Windows Task Scheduler:
    Program: python
    Arguments: D:\\athensbus-tracker\\scripts\\run_hourly.py
    Start in: D:\\athensbus-tracker
    Trigger: Daily, repeat every 1 hour
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db
import oasa_client as oasa
import sync_master_data
from sync_schedules import main as sync_schedules
from compute_daily_report import main as compute_report
from generate_site_data import main as generate_site

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        # Absolute path: Task Scheduler's CWD may differ from the repo root.
        RotatingFileHandler(str(Path(__file__).parent.parent / "run_hourly.log"),
                            maxBytes=5_000_000, backupCount=2, encoding="utf-8"),
    ],
    # Imported modules (sync_schedules etc.) call basicConfig at import time,
    # which would otherwise make this call a no-op — the file handler was never
    # attached and run_hourly.log stayed empty. force=True reconfigures root.
    force=True,
)
log = logging.getLogger("run_hourly")

# Root of the repo (one level up from scripts/)
REPO_ROOT = str(Path(__file__).parent.parent)



def _master_sync_due(conn) -> bool:
    """True αν ο τελευταίος επιτυχής master sync είναι >7 ημέρες πίσω (ή δεν
    έχει γίνει ποτέ) — γραμμές/διαδρομές/στάσεις ανανεώνονται εβδομαδιαία."""
    row = conn.execute(
        "SELECT MAX(started_at) m FROM job_runs "
        "WHERE job_name='sync_master_data' AND status='success'").fetchone()
    if not row or not row["m"]:
        return True
    from datetime import datetime, timezone, timedelta
    try:
        last = datetime.fromisoformat(row["m"])
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last > timedelta(days=7)

def schedule_already_synced_today(conn) -> bool:
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) c FROM scheduled_trips WHERE schedule_date=?", (today,)
    ).fetchone()
    return (row["c"] or 0) > 0


def git_commit_and_push() -> bool:
    """Commit db + docs/data and push to GitHub. Returns True on success."""
    try:
        def run(cmd):
            # encoding="utf-8" (with errors="replace") is required on Windows:
            # the default console codec (cp1253) cannot decode git's UTF-8
            # output (Greek commit messages) and crashes the reader thread.
            result = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace"
            )
            if result.returncode != 0:
                log.warning("git command failed: %s\n%s", " ".join(cmd), result.stderr)
            return result.returncode == 0

        run(["git", "config", "user.name",  "athensbus-bot"])
        run(["git", "config", "user.email", "actions@users.noreply.github.com"])
        # Only the generated site data goes to git. The sqlite DB stays local:
        # it grew past GitHub's 100MB file limit and pushes were rejected.
        run(["git", "add", "docs/data/"])

        # Check if there's anything to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=REPO_ROOT, capture_output=True
        )
        if result.returncode == 0:
            log.info("No changes to commit.")
            return True

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        run(["git", "commit", "-m", f"ωριαία ενημέρωση: {stamp}"])
        success = run(["git", "push", "origin", "main"])
        if success:
            log.info("Pushed to GitHub successfully.")
        return success

    except Exception as e:
        log.error("git push failed: %s", e)
        return False


def main():
    log.info("=== Hourly run started ===")
    db.ensure_schema()

    conn = db.get_connection()

    # Step 0: master data (γραμμές/διαδρομές/στάσεις) — αυτόματη εβδομαδιαία
    # ανανέωση, ώστε νέες γραμμές, αλλαγές διαδρομών και οι εποχιακές
    # μεταβάσεις (θερινό/χειμερινό) να πιάνονται χωρίς χειροκίνητο setup.
    if _master_sync_due(conn):
        log.info("Master data refresh (weekly)...")
        try:
            sync_master_data.main()
        except Exception as e:
            log.warning("Master sync failed (non-fatal): %s", e)

    # Step 1: sync today's schedule — EVERY hour (silent mirror + safety nets).
    conn.close()
    log.info("Syncing today's schedule...")
    try:
        sync_schedules.main()
    except Exception as e:
        log.warning("Schedule sync failed (non-fatal): %s", e)

    # Step 2: compute daily report (trips, slots, stats)
    log.info("Computing daily report...")
    try:
        compute_report()
    except Exception as e:
        log.error("Compute failed: %s", e)
        sys.exit(1)

    # Step 3: generate site JSON files
    log.info("Generating site data...")
    try:
        generate_site()
    except Exception as e:
        log.error("Site generation failed: %s", e)
        sys.exit(1)

    # Step 4: commit and push
    log.info("Pushing to GitHub...")
    git_commit_and_push()

    log.info("=== Hourly run complete ===")


if __name__ == "__main__":
    main()
