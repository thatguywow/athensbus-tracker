"""
merge_ping_artifacts.py — merges downloaded artifact files into the DB.

Handles two artifact types committed to the ping-artifacts branch:
  artifacts/pings-*/pings.jsonl     → vehicle_pings
  artifacts/terminus-*/terminus.jsonl → terminus_observations

Usage:
    python scripts/merge_ping_artifacts.py <artifacts_dir>
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("merge_ping_artifacts")


def merge_pings(conn, fpath: Path) -> tuple[int, int]:
    """Returns (inserted, duplicates)."""
    inserted = dupes = 0
    with open(fpath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
                cur = conn.execute("""
                    INSERT OR IGNORE INTO vehicle_pings
                        (route_code, vehicle_no, lat, lng, ts_utc, polled_at)
                    VALUES (?,?,?,?,?,?)
                """, (p["route_code"], p["vehicle_no"],
                      p["lat"], p["lng"], p["ts_utc"], p["polled_at"]))
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    dupes += 1
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
    return inserted, dupes


def merge_terminus(conn, fpath: Path) -> tuple[int, int]:
    """Returns (inserted, duplicates)."""
    inserted = dupes = 0
    with open(fpath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
                cur = conn.execute("""
                    INSERT OR IGNORE INTO terminus_observations
                        (route_code, stop_code, stop_type, vehicle_no,
                         predicted_mins, observed_at)
                    VALUES (?,?,?,?,?,?)
                """, (p["route_code"], p["stop_code"], p["stop_type"],
                      p.get("vehicle_no", ""),
                      int(p.get("predicted_mins", 0)),
                      p["observed_at"]))
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    dupes += 1
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
    return inserted, dupes


def main():
    if len(sys.argv) < 2:
        print("usage: python merge_ping_artifacts.py <artifacts_dir>", file=sys.stderr)
        sys.exit(1)

    root = Path(sys.argv[1])
    if not root.is_dir():
        log.warning("Artifacts dir %s does not exist — nothing to merge.", root)
        return

    db.ensure_schema()

    with db.job_run("merge_ping_artifacts") as run:
        conn = db.get_connection()
        try:
            ping_files     = list(root.rglob("pings.jsonl"))
            terminus_files = list(root.rglob("terminus.jsonl"))
            log.info("Found %d ping files, %d terminus files",
                     len(ping_files), len(terminus_files))

            total_pings_in = total_pings_dupes = 0
            total_term_in  = total_term_dupes  = 0

            for fpath in ping_files:
                ins, dup = merge_pings(conn, fpath)
                total_pings_in += ins
                total_pings_dupes += dup
                conn.commit()

            for fpath in terminus_files:
                ins, dup = merge_terminus(conn, fpath)
                total_term_in += ins
                total_term_dupes += dup
                conn.commit()

            run.detail = (
                f"ping_files={len(ping_files)} "
                f"pings_inserted={total_pings_in} pings_dupes={total_pings_dupes} "
                f"terminus_files={len(terminus_files)} "
                f"terminus_inserted={total_term_in} terminus_dupes={total_term_dupes}"
            )
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
