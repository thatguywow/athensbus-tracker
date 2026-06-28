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
from datetime import datetime, timezone, timedelta
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
MAX_WORKERS        = 8     # getBusLocation — gentler while the IP recovers from rate-limiting
STOP_MAX_WORKERS   = 6     # getStopArrivals — low concurrency like fragkakis, avoids 403
STOP_BATCH_SIZE    = 150   # spread stop polls in chunks across the cycle


CHECKPOINT_DEPTH = 2   # track first K and last K stops per route


def get_terminus_stops(conn) -> list[dict]:
    """
    Return the first CHECKPOINT_DEPTH and last CHECKPOINT_DEPTH stops of each
    route. Near-origin stops let us back-calculate the true departure time
    (the origin itself gives no arrival prediction on non-circular routes),
    and near-terminus stops give the arrival time.
    """
    rows = conn.execute("""
        SELECT route_code,
               MIN(stop_order) AS first_order,
               MAX(stop_order) AS last_order,
               COUNT(*)        AS n
        FROM stops GROUP BY route_code
    """).fetchall()

    checkpoints = []
    for r in rows:
        lo, hi, n = r["first_order"], r["last_order"], r["n"]
        if n < 3:
            continue
        wanted = set()
        for k in range(CHECKPOINT_DEPTH):
            wanted.add(lo + k)        # first K
            wanted.add(hi - k)        # last K
        for order in sorted(wanted):
            if order < lo or order > hi:
                continue
            sc = conn.execute(
                "SELECT stop_code FROM stops WHERE route_code=? AND stop_order=?",
                (r["route_code"], order)
            ).fetchone()
            if not sc:
                continue
            if order == lo:
                stype = "origin"
            elif order == hi:
                stype = "terminus"
            elif order <= lo + CHECKPOINT_DEPTH - 1:
                stype = "near_origin"
            else:
                stype = "near_terminus"
            checkpoints.append({
                "route_code": r["route_code"],
                "stop_code":  sc["stop_code"],
                "stop_type":  stype,
                "stop_order": order,
            })
    return checkpoints


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
                                polled_at: str, prev_state: dict) -> dict:
    """
    Disappearance detection (adapted from fragkakis/athensbus StopSyncer):

    For each tracked stop we remember the set of vehicles predicted to arrive
    in the PREVIOUS poll, with their predicted time. When a vehicle that was
    predicted last time is NO LONGER predicted now, it has PASSED the stop.
    Its exact pass time = previous_poll_time + btime2_minutes (OASA's own
    prediction), capped at now. This yields accurate departure (origin) and
    arrival (terminus) times even with multi-minute polling.

    prev_state is kept IN MEMORY across cycles (poller is long-running), so it
    adds zero git/storage overhead.
    """
    stop_codes = list({s["stop_code"] for s in terminus_stops})
    # Spread the polls: small chunks at low concurrency, brief pause between —
    # mirrors fragkakis (never bursts), which is what avoids the 403 rate-limit.
    ok_map: dict = {}
    for i in range(0, len(stop_codes), STOP_BATCH_SIZE):
        chunk = stop_codes[i:i + STOP_BATCH_SIZE]
        b = oasa.batch_get_stop_arrivals(chunk, max_workers=STOP_MAX_WORKERS)
        ok_map.update(b.ok)
        if i + STOP_BATCH_SIZE < len(stop_codes):
            time.sleep(1.0)

    class _B:  # adapt to the existing .ok interface below
        ok = ok_map
    batch = _B()

    stop_meta: dict[str, list[tuple[str, str, int]]] = {}
    for s in terminus_stops:
        stop_meta.setdefault(s["stop_code"], []).append(
            (s["route_code"], s["stop_type"], s["stop_order"]))

    now_dt = datetime.fromisoformat(polled_at)
    n_passages = 0

    for stop_code in stop_codes:
        arrivals = batch.ok.get(stop_code) or []

        current = {}
        for a in arrivals:
            veh = str(a.get("VEH_NO") or "")
            if not veh:
                continue
            try:
                bt = int(a.get("btime2") or a.get("time2") or 0)
            except (ValueError, TypeError):
                bt = 0
            current[veh] = {"btime2": bt, "route_code": str(a.get("route_code") or "")}

        prev = prev_state.get(stop_code)

        if prev:
            try:
                gap_min = (now_dt - datetime.fromisoformat(prev["polled_at"])).total_seconds()/60
            except Exception:
                gap_min = 999
            if gap_min <= 10:
                for veh, info in prev["vehicles"].items():
                    if veh in current:
                        continue  # still approaching
                    pass_dt = datetime.fromisoformat(prev["polled_at"]) + \
                              timedelta(minutes=info["btime2"])
                    if pass_dt > now_dt:
                        pass_dt = now_dt
                    pass_iso = pass_dt.isoformat()
                    service_date = _athens_date(pass_dt)
                    for (route_code, stop_type, stop_order) in stop_meta.get(stop_code, []):
                        if info["route_code"] and info["route_code"] != route_code:
                            continue
                        try:
                            conn.execute("""
                                INSERT OR IGNORE INTO stop_passages
                                    (route_code, stop_code, stop_type, stop_order,
                                     vehicle_no, passed_at, service_date, recorded_at)
                                VALUES (?,?,?,?,?,?,?,?)
                            """, (route_code, stop_code, stop_type, stop_order,
                                  veh, pass_iso, service_date, polled_at))
                            n_passages += 1
                        except Exception:
                            pass

        prev_state[stop_code] = {"polled_at": polled_at, "vehicles": current}

    conn.commit()
    return {"passages": n_passages}


def _athens_date(dt_utc: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.astimezone(ZoneInfo("Europe/Athens")).date().isoformat()
    except Exception:
        return dt_utc.date().isoformat()


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
    # In-memory previous predictions per stop (for disappearance detection).
    # Lives only in this long-running process → zero storage/git overhead.
    prev_arrival_state: dict = {}

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

            # Poll endpoint arrivals → disappearance detection for exact times
            if terminus_stops:
                term_stats = collect_and_store_terminus(
                    conn, terminus_stops, polled_at, prev_arrival_state)
                log.info("Exact stop passages detected: %d", term_stats["passages"])

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
