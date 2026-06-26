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
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db
import oasa_client as oasa
from sync_schedules import main as sync_schedules
from compute_daily_report import main as compute_report
from generate_site_data import main as generate_site

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("run_hourly.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("run_hourly")

# Root of the repo (one level up from scripts/)
REPO_ROOT = str(Path(__file__).parent.parent)


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
            result = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, text=True
            )
            if result.returncode != 0:
                log.warning("git command failed: %s\n%s", " ".join(cmd), result.stderr)
            return result.returncode == 0

        run(["git", "config", "user.name",  "athensbus-bot"])
        run(["git", "config", "user.email", "actions@users.noreply.github.com"])
        run(["git", "add", "db/athensbus.db", "docs/data/"])

        # Check if there's anything to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=REPO_ROOT, capture_output=True
        )
        if result.returncode == 0:
            log.info("No changes to commit.")
            return True

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        run(["git", "commit", "-m", f"hourly update: {stamp}"])
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

    # Step 1: sync today's schedule if not done yet
    if not schedule_already_synced_today(conn):
        log.info("Syncing today's schedule...")
        conn.close()
        try:
            sync_schedules()
        except Exception as e:
            log.warning("Schedule sync failed (non-fatal): %s", e)
        conn = db.get_connection()
    else:
        log.info("Schedule already synced today, skipping.")

    conn.close()

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
