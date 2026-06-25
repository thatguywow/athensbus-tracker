"""
poll_live.py — runs every ~5 minutes via GitHub Actions schedule.

Each run is STATELESS with respect to the committed database: it reads the
list of known route_codes from the (read-only, committed) SQLite file, polls
getBusLocation for all of them concurrently, and writes the resulting pings
to a flat JSONL file (one JSON object per line) rather than inserting them
into SQLite directly. That file is then uploaded as a short-lived GitHub
Actions artifact by the workflow — see poll-live.yml for why.

Output location: $ATHENSBUS_PINGS_OUT, or /tmp/pings/pings.jsonl by default.
Each line: {"route_code": "...", "vehicle_no": "...", "lat": ..., "lng": ...,
            "ts_utc": "...", "polled_at": "..."}

Local/manual use: if you just want to smoke-test connectivity without the
artifact pipeline, run this directly and inspect the JSONL output, or pass
--db-write to insert straight into the local SQLite file (used by tests).
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import db
import oasa_client as oasa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poll_live")

MAX_WORKERS = 16
DEFAULT_OUT = "/tmp/pings/pings.jsonl"


def collect_pings(route_codes: list[str], polled_at: str) -> tuple[list[dict], dict]:
    """Returns (pings, run_stats). Never raises on partial failure."""
    batch = oasa.batch_get_bus_locations(route_codes, max_workers=MAX_WORKERS)

    pings = []
    n_parse_errors = 0
    for route_code, vehicles in batch.ok.items():
        for v in vehicles or []:
            try:
                pings.append({
                    "route_code": route_code,
                    "vehicle_no": str(v["VEH_NO"]),
                    "lat": float(v["CS_LAT"]),
                    "lng": float(v["CS_LNG"]),
                    "ts_utc": oasa.parse_oasa_date(v["CS_DATE"]),
                    "polled_at": polled_at,
                })
            except (KeyError, ValueError, TypeError):
                n_parse_errors += 1

    stats = {
        "routes_total": len(route_codes),
        "routes_ok": batch.success_count,
        "routes_failed": batch.failure_count,
        "pings": len(pings),
        "parse_errors": n_parse_errors,
        "sample_failures": dict(list(batch.failed.items())[:5]),
    }
    return pings, stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-write", action="store_true",
        help="insert directly into the local SQLite DB instead of writing JSONL (used for local testing)",
    )
    args = parser.parse_args()

    db.ensure_schema()
    polled_at = db.now_utc_iso()

    with db.job_run("poll_live") as run:
        conn = db.get_connection()
        try:
            route_rows = conn.execute("SELECT route_code FROM routes").fetchall()
            route_codes = [r["route_code"] for r in route_rows]

            if not route_codes:
                run.status = "error"
                run.detail = "no routes in DB — run sync_master_data.py first"
                log.error(run.detail)
                return

            log.info("Polling %d routes...", len(route_codes))
            pings, stats = collect_pings(route_codes, polled_at)

            if args.db_write:
                for p in pings:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO vehicle_pings (route_code, vehicle_no, lat, lng, ts_utc, polled_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (p["route_code"], p["vehicle_no"], p["lat"], p["lng"], p["ts_utc"], p["polled_at"]),
                    )
                conn.commit()
            else:
                out_path = os.environ.get("ATHENSBUS_PINGS_OUT", DEFAULT_OUT)
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    for p in pings:
                        f.write(json.dumps(p, separators=(",", ":")) + "\n")
                log.info("Wrote %d pings to %s", len(pings), out_path)

            run.detail = (
                f"routes_ok={stats['routes_ok']} routes_failed={stats['routes_failed']} "
                f"pings={stats['pings']} parse_errors={stats['parse_errors']}"
            )
            failure_rate = stats["routes_failed"] / max(1, stats["routes_total"])
            if failure_rate > 0.15:
                run.status = "partial"
                log.warning("High failure rate (%.1f%%). Sample: %s", failure_rate * 100, stats["sample_failures"])
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()

