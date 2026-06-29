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
import queue
import threading
from collections import defaultdict
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

# ── Two-speed spread poller ─────────────────────────────────────────────────
# Edge stops (first/last EDGE_DEPTH of each route) are polled densely so we
# catch fast early/late passages and get accurate departure/arrival. Middle
# stops (optional) are spread thinly over MIDDLE_WINDOW. A global rate cap keeps
# us under the IP request limit; the spread means we never burst.
EDGE_DEPTH      = 3      # first/last K stops per route (where accuracy matters)
EDGE_INTERVAL   = 90     # target seconds between polls of each edge stop
ENABLE_MIDDLE   = False  # poll middle stops too (fragkakis-style); off until needed
MIDDLE_WINDOW   = 300    # seconds between polls of each middle stop
TARGET_RATE     = 10     # max getStopArrivals/sec (safety cap) — raise if no 403s
STOP_WORKERS    = 8      # getStopArrivals fetch threads
LOC_WORKERS     = 8      # getBusLocation fetch threads
GBL_INTERVAL    = 300    # getBusLocation sweep every N seconds (GPS pings)
DISAPPEAR_GUARD_MINS = 10
COMMIT_EVERY_SECS    = 2.0
LOG_EVERY_SECS       = 60
WORK_Q_MAX           = 20000

CHECKPOINT_DEPTH = EDGE_DEPTH   # get_terminus_stops uses this


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


def get_middle_stops(conn) -> list[dict]:
    """All stops that are NOT edges (between first EDGE_DEPTH and last EDGE_DEPTH)."""
    rows = conn.execute("""
        SELECT route_code, MIN(stop_order) AS lo, MAX(stop_order) AS hi
        FROM stops GROUP BY route_code
    """).fetchall()
    out = []
    for r in rows:
        lo, hi = r["lo"], r["hi"]
        srows = conn.execute(
            "SELECT stop_order, stop_code FROM stops WHERE route_code=? ORDER BY stop_order",
            (r["route_code"],)).fetchall()
        for s in srows:
            o = s["stop_order"]
            if lo + EDGE_DEPTH <= o <= hi - EDGE_DEPTH:
                out.append({"route_code": r["route_code"], "stop_code": s["stop_code"],
                            "stop_type": "middle", "stop_order": o})
    return out


def build_stop_meta(stops: list[dict]) -> dict:
    """stop_code -> [(route_code, stop_type, stop_order), ...]"""
    meta: dict[str, list] = defaultdict(list)
    for s in stops:
        meta[s["stop_code"]].append((s["route_code"], s["stop_type"], s["stop_order"]))
    return dict(meta)


def build_buckets(stop_codes: list[str], window: int) -> dict[int, list[str]]:
    """Spread stop codes evenly across `window` one-second slots."""
    buckets: dict[int, list[str]] = defaultdict(list)
    n = max(1, len(stop_codes))
    for i, sc in enumerate(stop_codes):
        buckets[(i * window) // n % window].append(sc)
    return dict(buckets)


class RateLimiter:
    """Token bucket: at most `rate` acquisitions per second across all threads."""
    def __init__(self, rate: float):
        self.rate = float(rate)
        self.allowance = float(rate)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.allowance += (now - self.last) * self.rate
                self.last = now
                if self.allowance > self.rate:
                    self.allowance = self.rate
                if self.allowance >= 1.0:
                    self.allowance -= 1.0
                    return
            time.sleep(0.01)


def _stop_worker(work_q, result_q, limiter, stop_event):
    while not stop_event.is_set():
        try:
            stop_code = work_q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            limiter.acquire()                       # global rate cap
            poll_iso = oasa.now_utc_iso()
            arrivals = oasa.get_stop_arrivals(stop_code) or []
            current = {}
            for a in arrivals:
                veh = str(a.get("veh_code") or a.get("VEH_NO") or "")
                if not veh:
                    continue
                try:
                    bt = int(a.get("btime2") or a.get("btime") or 0)
                except (ValueError, TypeError):
                    bt = 0
                current[veh] = {"btime2": bt, "route_code": str(a.get("route_code") or "")}
            result_q.put(("arrival", stop_code, current, poll_iso))
        except Exception:
            pass
        finally:
            work_q.task_done()


def _loc_worker(loc_q, result_q, stop_event):
    while not stop_event.is_set():
        try:
            route_code = loc_q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            poll_iso = oasa.now_utc_iso()
            data = oasa.get_bus_location(route_code) or []
            result_q.put(("location", route_code, data, poll_iso))
        except Exception:
            pass
        finally:
            loc_q.task_done()


def _writer_thread(result_q, stop_meta, stats, stop_event):
    conn = db.get_connection()
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    prev: dict[str, dict] = {}
    last_commit = time.time()

    while not (stop_event.is_set() and result_q.empty()):
        try:
            item = result_q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            if item[0] == "arrival":
                _, stop_code, current, poll_iso = item
                now_dt = datetime.fromisoformat(poll_iso)
                p = prev.get(stop_code)
                if p:
                    try:
                        gap = (now_dt - datetime.fromisoformat(p["polled_at"])).total_seconds()/60
                    except Exception:
                        gap = 999
                    if gap <= DISAPPEAR_GUARD_MINS:
                        for veh, info in p["vehicles"].items():
                            if veh in current:
                                continue
                            pass_dt = datetime.fromisoformat(p["polled_at"]) + \
                                      timedelta(minutes=info["btime2"])
                            if pass_dt > now_dt:
                                pass_dt = now_dt
                            pass_iso = pass_dt.isoformat()
                            sd = _athens_date(pass_dt)
                            for (rc, stype, order) in stop_meta.get(stop_code, []):
                                if info["route_code"] and info["route_code"] != rc:
                                    continue
                                try:
                                    conn.execute("""
                                        INSERT OR IGNORE INTO stop_passages
                                            (route_code, stop_code, stop_type, stop_order,
                                             vehicle_no, passed_at, service_date, recorded_at)
                                        VALUES (?,?,?,?,?,?,?,?)
                                    """, (rc, stop_code, stype, order, veh, pass_iso, sd, poll_iso))
                                    stats["passages"] += 1
                                except Exception:
                                    pass
                prev[stop_code] = {"polled_at": poll_iso, "vehicles": current}
            else:
                _, route_code, vehicles, poll_iso = item
                for v in (vehicles or []):
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO vehicle_pings
                                (route_code, vehicle_no, lat, lng, ts_utc, polled_at)
                            VALUES (?,?,?,?,?,?)
                        """, (route_code, str(v["VEH_NO"]), float(v["CS_LAT"]),
                              float(v["CS_LNG"]), oasa.parse_oasa_date(v["CS_DATE"]), poll_iso))
                        stats["pings"] += 1
                    except (KeyError, ValueError, TypeError):
                        pass
        except Exception as e:
            log.error("writer error: %s", e)
        finally:
            result_q.task_done()
        if time.time() - last_commit > COMMIT_EVERY_SECS:
            try: conn.commit()
            except Exception: pass
            last_commit = time.time()

    try:
        conn.commit(); conn.close()
    except Exception:
        pass


def _athens_date(dt_utc: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.astimezone(ZoneInfo("Europe/Athens")).date().isoformat()
    except Exception:
        return dt_utc.date().isoformat()


def main():
    db.ensure_schema()
    conn = db.get_connection()
    route_codes = [r["route_code"] for r in
                   conn.execute("SELECT route_code FROM routes").fetchall()]
    edges = get_terminus_stops(conn)
    middles = get_middle_stops(conn) if ENABLE_MIDDLE else []
    conn.close()

    if not route_codes:
        log.error("No routes in DB. Run first_time_setup first.")
        sys.exit(1)

    stop_meta = build_stop_meta(edges + middles)
    edge_codes = sorted({s["stop_code"] for s in edges})
    middle_codes = sorted({s["stop_code"] for s in middles}) if ENABLE_MIDDLE else []
    edge_buckets = build_buckets(edge_codes, EDGE_INTERVAL)
    middle_buckets = build_buckets(middle_codes, MIDDLE_WINDOW) if ENABLE_MIDDLE else {}

    implied_rate = len(edge_codes) / max(1, EDGE_INTERVAL)
    log.info("Two-speed poller: %d routes | %d edge stops (every %ds → ~%.1f/s) | "
             "%d middle stops | rate cap %d/s",
             len(route_codes), len(edge_codes), EDGE_INTERVAL, implied_rate,
             len(middle_codes), TARGET_RATE)
    if implied_rate > TARGET_RATE:
        log.warning("Edge demand ~%.1f/s exceeds cap %d/s → edges will poll slower "
                    "than %ds. Raise TARGET_RATE if no 403s, or raise EDGE_INTERVAL.",
                    implied_rate, TARGET_RATE, EDGE_INTERVAL)

    work_q = queue.Queue(maxsize=WORK_Q_MAX)
    loc_q = queue.Queue(maxsize=5000)
    result_q = queue.Queue(maxsize=50000)
    limiter = RateLimiter(TARGET_RATE)
    stats = {"passages": 0, "pings": 0, "skipped": 0}
    stop_event = threading.Event()

    threads = []
    for _ in range(STOP_WORKERS):
        t = threading.Thread(target=_stop_worker, args=(work_q, result_q, limiter, stop_event), daemon=True)
        t.start(); threads.append(t)
    for _ in range(LOC_WORKERS):
        t = threading.Thread(target=_loc_worker, args=(loc_q, result_q, stop_event), daemon=True)
        t.start(); threads.append(t)
    writer = threading.Thread(target=_writer_thread, args=(result_q, stop_meta, stats, stop_event), daemon=True)
    writer.start()

    start = time.monotonic()
    last_sec = -1
    last_gbl = -GBL_INTERVAL
    last_log = time.time()

    try:
        while True:
            now = time.monotonic() - start
            cur = int(now)
            while last_sec < cur:
                last_sec += 1
                for sc in edge_buckets.get(last_sec % EDGE_INTERVAL, []):
                    try: work_q.put_nowait(sc)
                    except queue.Full: stats["skipped"] += 1
                if ENABLE_MIDDLE:
                    for sc in middle_buckets.get(last_sec % MIDDLE_WINDOW, []):
                        try: work_q.put_nowait(sc)
                        except queue.Full: stats["skipped"] += 1

            if now - last_gbl >= GBL_INTERVAL:
                last_gbl = now
                for rc in route_codes:
                    try: loc_q.put_nowait(rc)
                    except queue.Full: pass

            if time.time() - last_log >= LOG_EVERY_SECS:
                log.info("two-speed: passages=%d pings=%d skipped=%d  queue(stop=%d loc=%d result=%d)",
                         stats["passages"], stats["pings"], stats["skipped"],
                         work_q.qsize(), loc_q.qsize(), result_q.qsize())
                try:
                    with db.job_run("local_poll") as run:
                        run.detail = (f"passages={stats['passages']} pings={stats['pings']} "
                                      f"skipped={stats['skipped']} qlag={work_q.qsize()}")
                except Exception:
                    pass
                last_log = time.time()

            time.sleep(0.05)
    except KeyboardInterrupt:
        log.info("Stopping (Ctrl-C). Flushing…")
        stop_event.set()
        writer.join(timeout=10)


if __name__ == "__main__":
    main()
