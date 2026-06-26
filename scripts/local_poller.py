"""
local_poller.py — runs continuously on your LOCAL machine.

Every 5 minutes:
  - getBusLocation for all routes → writes directly to local SQLite DB
  - getStopArrivals for terminus stops → writes to local SQLite DB

No GitHub interaction — just pure local data collection.
The hourly push job (run_hourly.bat) handles committing to GitHub.

Usage:
    python scripts/local_poller.py

Leave this running at all times. Stop with Ctrl+C.
On Windows: add to Task Scheduler as "run at startup" or just
leave a terminal open with run_poller.bat.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db
import oasa_client as oasa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("local_poller.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("local_poller")

POLL_INTERVAL_SECS = 300   # 5 minutes
MAX_WORKERS        = 16


def get_terminus_stops(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT route_code,
               MIN(stop_order) AS first_order,
               MAX(stop_order) AS last_order
        FROM stops GROUP BY route_code
    """).fetchall()

    terminus_stops = []
    for r in rows:
        for order, stype in [(r["first_order"], "origin"),
                              (r["last_order"],  "terminus")]:
            sc = conn.execute(
                "SELECT stop_code FROM stops WHERE route_code=? AND stop_order=?",
                (r["route_code"], order)
            ).fetchone()
            if sc:
                terminus_stops.append({
                    "route_code": r["route_code"],
                    "stop_code":  sc["stop_code"],
                    "stop_type":  stype,
                })
    return terminus_stops


def collect_and_store_pings(conn, route_codes: list[str], polled_at: str) -> dict:
    batch = oasa.batch_get_bus_locations(route_codes, max_workers=MAX_WORKERS)
    n_pings = parse_errors = 0

    for route_code, vehicles in batch.ok.items():
        for v in (vehicles or []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO vehicle_pings
                        (route_code, vehicle_no, lat, lng, ts_utc, polled_at)
                    VALUES (?,?,?,?,?,?)
                """, (route_code, str(v["VEH_NO"]),
                      float(v["CS_LAT"]), float(v["CS_LNG"]),
                      oasa.parse_oasa_date(v["CS_DATE"]), polled_at))
                n_pings += 1
            except (KeyError, ValueError, TypeError):
                parse_errors += 1

    conn.commit()
    return {
        "routes_ok":     batch.success_count,
        "routes_failed": batch.failure_count,
        "pings":         n_pings,
        "parse_errors":  parse_errors,
    }


def collect_and_store_terminus(conn, terminus_stops: list[dict],
                                polled_at: str) -> int:
    stop_codes = list({s["stop_code"] for s in terminus_stops})
    batch = oasa.batch_get_stop_arrivals(stop_codes, max_workers=MAX_WORKERS)
    n_obs = 0

    for stop in terminus_stops:
        arrivals = batch.ok.get(stop["stop_code"], [])
        for a in (arrivals or []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO terminus_observations
                        (route_code, stop_code, stop_type, vehicle_no,
                         predicted_mins, observed_at)
                    VALUES (?,?,?,?,?,?)
                """, (stop["route_code"], stop["stop_code"], stop["stop_type"],
                      str(a.get("VEH_NO") or ""),
                      int(a.get("btime2") or a.get("time2") or 0),
                      polled_at))
                n_obs += 1
            except (KeyError, ValueError, TypeError):
                pass

    conn.commit()
    return n_obs


def main():
    db.ensure_schema()
    log.info("Local poller started. Polling every %ds.", POLL_INTERVAL_SECS)

    # Load route list and terminus stops
    conn = db.get_connection()
    route_codes = [r["route_code"] for r in
                   conn.execute("SELECT route_code FROM routes").fetchall()]
    terminus_stops = get_terminus_stops(conn)
    conn.close()

    if not route_codes:
        log.error("No routes in DB. Run first_time_setup.bat first.")
        sys.exit(1)

    log.info("Loaded %d routes, %d terminus stops.", len(route_codes), len(terminus_stops))

    last_reload = time.time()

    while True:
        cycle_start = time.time()
        polled_at   = oasa.now_utc_iso()

        try:
            conn = db.get_connection()

            # Poll vehicle positions
            ping_stats = collect_and_store_pings(conn, route_codes, polled_at)
            log.info("Pings: routes_ok=%d routes_failed=%d pings=%d",
                     ping_stats["routes_ok"], ping_stats["routes_failed"],
                     ping_stats["pings"])

            # Poll terminus arrivals
            if terminus_stops:
                n_obs = collect_and_store_terminus(conn, terminus_stops, polled_at)
                log.info("Terminus observations: %d", n_obs)

            # Log to job_runs for pipeline health visibility
            with db.job_run("local_poll") as run:
                run.detail = (
                    f"routes_ok={ping_stats['routes_ok']} "
                    f"routes_failed={ping_stats['routes_failed']} "
                    f"pings={ping_stats['pings']}"
                )

            conn.close()

        except Exception as e:
            log.error("Poll cycle error: %s", e, exc_info=True)

        # Reload route list every hour in case master data was synced
        if time.time() - last_reload > 3600:
            conn = db.get_connection()
            route_codes = [r["route_code"] for r in
                           conn.execute("SELECT route_code FROM routes").fetchall()]
            terminus_stops = get_terminus_stops(conn)
            conn.close()
            last_reload = time.time()
            log.info("Reloaded: %d routes, %d terminus stops",
                     len(route_codes), len(terminus_stops))

        elapsed = time.time() - cycle_start
        sleep   = max(0, POLL_INTERVAL_SECS - elapsed)
        log.info("Cycle done in %.1fs. Sleeping %.1fs.", elapsed, sleep)
        time.sleep(sleep)


if __name__ == "__main__":
    main()
