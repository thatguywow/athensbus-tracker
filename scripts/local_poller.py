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
from logging.handlers import RotatingFileHandler
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
        RotatingFileHandler("local_poller.log", maxBytes=5_000_000, backupCount=2, encoding="utf-8"),
    ],
)
log = logging.getLogger("local_poller")

# ── Two-speed spread poller ─────────────────────────────────────────────────
# Edge stops (first/last EDGE_DEPTH of each route) are polled round-robin as
# fast as TARGET_RATE allows, so each is polled every (num_stops / TARGET_RATE)
# seconds. The feeder self-paces to worker throughput — the queue never backs
# up. TARGET_RATE is the single knob: raise for denser polling (more accuracy)
# if you see no 403s, lower it if 403s are heavy.
EDGE_DEPTH      = 3      # first/last K stops per route (where accuracy matters)
ENABLE_MIDDLE   = False  # also poll middle stops (fragkakis-style); off until needed
TARGET_RATE     = 55     # max total requests/sec — the main knob. 25 → 40 → 55
                         # after the TLS fix cut poller CPU from 89% to 19% of
                         # one core (36% at 40/s, no 403s). Cycle ≈ 29s,
                         # which matters for COVERAGE too: stops 2-3 sit 1-2 min
                         # past the origin, so a shorter cycle turns "sometimes
                         # caught" into "almost always caught". Lower again if
                         # 403s stop being rare.
STOP_WORKERS    = 24     # getStopArrivals fetch threads. At p50≈0.26s each
                         # worker sustains ~3.8 req/s, so 55/s needs ≥15; 24
                         # leaves headroom for slow responses. BURST_CAP stays
                         # at 5 — OASA tolerates a faster STEADY rate, not bursts.
DISAPPEAR_GUARD_MINS = 10
PASS_TRUST_MINS      = 5     # a disappearance counts as a passage only if the
                             # vehicle was ≤ this close (btime2) when last seen;
                             # further away = withdrawn/cancelled, not passed
COMMIT_EVERY_SECS    = 2.0
LOG_EVERY_SECS       = 60
JOB_RUN_EVERY_SECS   = 900   # job_runs εγγραφή κάθε 15' (το log μένει ανά λεπτό)

ORIGIN_DEPTH_MAX = 8     # πόσο βαθιά ψάχνουμε αποδοτικές στάσεις στην αφετηρία
YIELD_KEEP       = 0.30  # διελεύσεις/δρομολόγιο ≥ αυτό ⇒ η στάση κρατιέται
YIELD_DROP       = 0.10  # < αυτό (μετρημένο) ⇒ υποβιβασμός σε scout
MIN_TRIPS_FOR_YIELD = 5  # λιγότερα δρομολόγια 7 ημερών ⇒ δεν κρίνουμε ακόμα
SCOUT_EVERY      = 10    # οι υποβιβασμένες στάσεις ελέγχονται 1 κύκλο στους 10
SCOUT_PROMOTE_BTIME = 2  # #8: scout στάση που δείχνει όχημα ≤2′ μακριά αποδεικνύει
                         # ότι δουλεύει ⇒ προάγεται αμέσως (η αραιή δειγματοληψία
                         # δεν θα την άφηνε ποτέ να περάσει το κατώφλι απόδοσης)

CHECKPOINT_DEPTH = EDGE_DEPTH   # get_terminus_stops uses this


def measure_origin_yield(conn, days: int = 7) -> dict:
    """
    (route_code, stop_code) → passages per trip over the last `days`.

    Some terminals publish no departure predictions at all: on lines 815, Α5,
    224, 140, Α8… the origin stop yields ZERO passages while the terminus
    yields hundreds, so 24-36% of their trips have no observed departure. The
    stop is polled thousands of times a day for nothing. This metric lets the
    poller spend that budget on the first stops that DO answer.

    Keyed on stop_code (not stop_order) so a route reshuffle cannot corrupt the
    history.
    """
    trips = {r["route_code"]: r["n"] for r in conn.execute("""
        SELECT route_code, COUNT(*) n FROM trips
        WHERE service_date >= date('now', ?) GROUP BY route_code
    """, (f"-{days} day",))}
    out: dict[tuple, float] = {}
    for r in conn.execute("""
        SELECT route_code, stop_code, COUNT(*) n FROM stop_passages
        WHERE passed_at >= date('now', ?) GROUP BY route_code, stop_code
    """, (f"-{days} day",)):
        t = trips.get(r["route_code"], 0)
        if t >= MIN_TRIPS_FOR_YIELD:
            out[(r["route_code"], r["stop_code"])] = r["n"] / t
    # `judgeable`: routes with enough trips for a silent stop to MEAN something.
    judgeable = {rc for rc, n in trips.items() if n >= MIN_TRIPS_FOR_YIELD}
    promoted = {(r["route_code"], r["stop_code"]) for r in conn.execute(
        "SELECT route_code, stop_code FROM stop_promotions "
        "WHERE seen_at >= date('now', ?)", (f"-{days} day",))}
    return {"yield": out, "judgeable": judgeable, "promoted": promoted}


def select_origin_stops(conn, route_code: str, lo: int, hi: int,
                        metrics: dict) -> tuple[list, list]:
    """
    Choose which origin-side stops to poll for one route: (main, scout).

    Key distinction: a stop inside the CURRENTLY polled depth (first EDGE_DEPTH)
    with no passages has been asked thousands of times and answered nothing —
    that is a MEASURED zero, not missing data. Stops beyond that depth have
    never been polled, so they are unknown and worth exploring.

    • main  — first EDGE_DEPTH stops that either produce passages (≥ YIELD_KEEP)
      or are still unexplored.
    • scout — stops measured dead. Polled once every SCOUT_EVERY cycles so they
      return automatically if OASA starts publishing departures there.

    A route whose origin side is entirely unexplored keeps the classic
    first-EDGE_DEPTH choice, so a fresh install behaves exactly as before.
    """
    cands = [(r["stop_code"], r["stop_order"]) for r in conn.execute(
        "SELECT stop_code, stop_order FROM stops WHERE route_code=? "
        "AND stop_order>=? AND stop_order<=? ORDER BY stop_order",
        (route_code, lo, min(lo + ORIGIN_DEPTH_MAX - 1, hi)))]
    if not cands:
        return [], []

    yields = metrics.get("yield", {})
    judgeable = route_code in metrics.get("judgeable", set())
    promoted = metrics.get("promoted", set())
    polled_depth = lo + EDGE_DEPTH - 1
    scored = []
    for sc, order in cands:
        y = yields.get((route_code, sc))
        if y is None and order <= polled_depth and judgeable:
            # Polled all week on a route that ran trips, yet silent ⇒ measured
            # dead. Without `judgeable` a quiet route would look dead too.
            y = 0.0
        scored.append((sc, order, y))

    # Nothing measurable yet (no trips in the window) → classic behaviour.
    if all(y is None for _sc, _o, y in scored):
        return [sc for sc, _o in cands[:EDGE_DEPTH]], []

    main, scout = [], []
    for sc, _order, y in scored:
        if len(main) >= EDGE_DEPTH:
            if y is not None and y < YIELD_DROP:
                scout.append(sc)
            continue
        if (route_code, sc) in promoted or y is None or y >= YIELD_KEEP:
            main.append(sc)            # promoted by evidence, productive, or unexplored
        else:
            scout.append(sc)           # measured dead → scout only
    if not main:                       # never leave a route unwatched
        main = [sc for sc, _o in cands[:EDGE_DEPTH]]
        scout = []
    return main, scout


def get_terminus_stops(conn) -> list[dict]:
    """
    Return the first CHECKPOINT_DEPTH and last CHECKPOINT_DEPTH stops of each
    route. Near-origin stops let us back-calculate the true departure time
    (the origin itself gives no arrival prediction on non-circular routes),
    and near-terminus stops give the arrival time.

    The origin side is chosen ADAPTIVELY (see select_origin_stops): dead
    terminals are demoted to a low-frequency scout list and the freed budget
    goes to the first stops that answer. The terminus side is unchanged — it
    works everywhere.
    """
    metrics = measure_origin_yield(conn)
    rows = conn.execute("""
        SELECT route_code,
               MIN(stop_order) AS first_order,
               MAX(stop_order) AS last_order,
               COUNT(*)        AS n
        FROM stops GROUP BY route_code
    """).fetchall()

    checkpoints = []
    scouts = []
    for r in rows:
        lo, hi, n = r["first_order"], r["last_order"], r["n"]
        if n < 3:
            continue
        rc = r["route_code"]
        main_codes, scout_codes = select_origin_stops(conn, rc, lo, hi, metrics)
        order_of = {row["stop_code"]: row["stop_order"] for row in conn.execute(
            "SELECT stop_code, stop_order FROM stops WHERE route_code=?", (rc,))}

        for sc in main_codes:
            order = order_of.get(sc, lo)
            checkpoints.append({
                "route_code": rc, "stop_code": sc,
                "stop_type": "origin" if order == lo else "near_origin",
                "stop_order": order,
            })
        for sc in scout_codes:
            order = order_of.get(sc, lo)
            scouts.append({
                "route_code": rc, "stop_code": sc,
                "stop_type": "origin" if order == lo else "near_origin",
                "stop_order": order,
            })

        # terminus side: unchanged
        for k in range(CHECKPOINT_DEPTH):
            order = hi - k
            if order < lo or order in (order_of.get(sc) for sc in main_codes):
                continue
            sc = conn.execute(
                "SELECT stop_code FROM stops WHERE route_code=? AND stop_order=?",
                (rc, order)).fetchone()
            if not sc:
                continue
            checkpoints.append({
                "route_code": rc, "stop_code": sc["stop_code"],
                "stop_type": "terminus" if order == hi else "near_terminus",
                "stop_order": order,
            })
    return checkpoints, scouts


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


def _feeder(cycle_stops: list[str], work_q: queue.Queue, stop_event: threading.Event,
            scout_stops: list[str] | None = None):
    """
    Round-robin feed: hand the next stop to the workers, blocking when the small
    work queue is full. This makes the poll rate self-pace to actual worker
    throughput — the queue never backs up, and each stop is polled every
    (len(cycle_stops) / effective_rate) seconds, automatically.
    """
    if not cycle_stops:
        return
    scout_stops = scout_stops or []
    i, n, passes = 0, len(cycle_stops), 0
    while not stop_event.is_set():
        try:
            work_q.put(cycle_stops[i % n], timeout=1.0)
            i += 1
            if i % n == 0:                       # completed a full cycle
                passes += 1
                if scout_stops and passes % SCOUT_EVERY == 0:
                    # Demoted stops get one pass in SCOUT_EVERY: if OASA starts
                    # publishing departures there, the yield rises and they are
                    # promoted back on the next rebuild. ~1% of the budget.
                    for sc in scout_stops:
                        if stop_event.is_set():
                            break
                        work_q.put(sc, timeout=1.0)
        except queue.Full:
            continue


class RateLimiter:
    """
    Token bucket: at most `rate` acquisitions per second across all threads.
    The bucket is capped at BURST_CAP tokens so a brief stall can't be followed
    by a burst of `rate` simultaneous requests (OASA is burst-sensitive).
    """
    BURST_CAP = 5.0

    def __init__(self, rate: float):
        self.rate = float(rate)
        self.cap = min(self.rate, self.BURST_CAP)
        self.allowance = self.cap
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.allowance += (now - self.last) * self.rate
                self.last = now
                if self.allowance > self.cap:
                    self.allowance = self.cap
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
                veh = _normalize_vehicle_no(veh)
                try:
                    bt = int(a.get("btime2") or a.get("btime") or 0)
                except (ValueError, TypeError):
                    bt = 0
                current[veh] = {"btime2": bt, "route_code": str(a.get("route_code") or "")}
            # #8: a stop that shows a vehicle about to pass is demonstrably
            # productive. The writer records it, and the next stop selection
            # promotes it back out of the scout list.
            close = sorted({v["route_code"] for v in current.values()
                            if v["btime2"] <= SCOUT_PROMOTE_BTIME and v["route_code"]})
            result_q.put(("arrival", stop_code, current, poll_iso, close))
        except Exception:
            pass
        finally:
            work_q.task_done()



def _writer_thread(result_q, stop_meta, stats, stop_event):
    conn = db.get_connection()
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    # per stop: last poll state + disappearances awaiting confirmation.
    # A vehicle must be missing from TWO consecutive polls before we record the
    # passage — a single miss is often an API glitch (vehicle reappears). The
    # pass TIME is unchanged (last-seen poll + btime2, capped at first miss), so
    # accuracy is unaffected; only the confirmation is delayed by one cycle.
    prev: dict[str, dict] = {}
    last_commit = time.time()

    def handle_arrival(stop_code, current, poll_iso):
        now_dt = datetime.fromisoformat(poll_iso)
        p = prev.get(stop_code)
        pending = p["pending"] if p else {}
        new_pending: dict[str, dict] = {}

        if p:
            # 1) confirm or cancel pending disappearances
            for veh, info in pending.items():
                if veh in current:
                    continue            # reappeared → glitch, drop
                if info["btime2"] > PASS_TRUST_MINS:
                    continue            # was far away when last seen → withdrawn,
                                        # not passed (e.g. pulled from service);
                                        # a truly approaching vehicle is re-seen
                                        # every cycle with shrinking btime2
                seen_dt = datetime.fromisoformat(info["seen_at"])
                if (now_dt - seen_dt).total_seconds() / 60 > DISAPPEAR_GUARD_MINS:
                    continue            # too stale to trust
                pass_dt = seen_dt + timedelta(minutes=info["btime2"])
                miss_dt = datetime.fromisoformat(info["miss_at"])
                if pass_dt > miss_dt:
                    # The vehicle passed somewhere in (last seen, first miss].
                    # OASA's prediction overshoots that window, so instead of
                    # pinning to the window END (systematic lateness up to a
                    # full cycle), estimate the MIDPOINT: zero average bias,
                    # half the worst-case error. Nothing else changes — same
                    # passage, same ordering, just a better timestamp.
                    pass_dt = seen_dt + (miss_dt - seen_dt) / 2
                pass_iso = pass_dt.isoformat()
                sd = _athens_date(pass_dt)
                for (rc, stype, order) in stop_meta.get(stop_code, []):
                    if info["route_code"] != rc:
                        continue        # strict route match
                    try:
                        c = conn.execute("""
                            INSERT OR IGNORE INTO stop_passages
                                (route_code, stop_code, stop_type, stop_order,
                                 vehicle_no, passed_at, service_date, recorded_at)
                            VALUES (?,?,?,?,?,?,?,?)
                        """, (rc, stop_code, stype, order, veh,
                              pass_iso, sd, poll_iso))
                        if c.rowcount > 0:
                            stats["passages"] += 1
                    except Exception:
                        pass

            # 2) fresh misses → pending (confirm on next poll)
            gap = (now_dt - datetime.fromisoformat(p["polled_at"])).total_seconds() / 60
            if gap <= DISAPPEAR_GUARD_MINS:
                for veh, info in p["vehicles"].items():
                    if veh in current or veh in pending:
                        continue
                    new_pending[veh] = {
                        "btime2":     info["btime2"],
                        "route_code": info["route_code"],
                        "seen_at":    p["polled_at"],
                        "miss_at":    poll_iso,
                    }

        prev[stop_code] = {"polled_at": poll_iso, "vehicles": current,
                           "pending": new_pending}

    while not (stop_event.is_set() and result_q.empty()):
        try:
            item = result_q.get(timeout=1.0)
        except queue.Empty:
            item = None
        if item is not None:
            try:
                if item[0] == "arrival":
                    _, stop_code, current, poll_iso = item[:4]
                    close = item[4] if len(item) > 4 else ()
                    handle_arrival(stop_code, current, poll_iso)
                    for rc in close:      # #8 scout promotion evidence
                        try:
                            conn.execute(
                                "INSERT INTO stop_promotions (route_code, stop_code, seen_at) "
                                "VALUES (?,?,?) ON CONFLICT(route_code, stop_code) "
                                "DO UPDATE SET seen_at=excluded.seen_at",
                                (rc, stop_code, poll_iso))
                        except Exception:
                            pass
            except Exception as e:
                log.error("writer error: %s", e)
            finally:
                result_q.task_done()
        # periodic commit — runs on idle too, so writes never sit unflushed
        if time.time() - last_commit > COMMIT_EVERY_SECS:
            try:
                conn.commit()
            except Exception:
                pass
            last_commit = time.time()

    try:
        conn.commit(); conn.close()
    except Exception:
        pass


from vehicle_classification import TROLLEY_RANGES


def _normalize_vehicle_no(veh: str) -> str:
    """
    OASA sends Κόκκινος Μύλος trolleys in TWO forms: the 4-digit fleet number
    (9012) and the legacy 8-prefixed 5-digit form (89012). Normalize to the
    4-digit fleet number so counting/classification stays consistent. Safe:
    no bus depot uses 5-digit numbers starting with 8, so a 5-digit 8XXXX
    whose remainder is a valid trolley number can only be a trolley.
    """
    if len(veh) == 5 and veh[0] == "8" and veh[1:].isdigit():
        rest = int(veh[1:])
        for lo, hi, _name in TROLLEY_RANGES:
            if lo <= rest <= hi:
                return veh[1:]
    return veh


def _athens_date(dt_utc: datetime) -> str:
    # Service day (04:00→04:00 Athens): passages before 04:00 belong to the
    # previous day's service — keeps night buses on the day their shift started.
    return db.athens_service_date(dt_utc)


def main():
    db.ensure_schema()
    conn = db.get_connection()
    route_codes = [r["route_code"] for r in
                   conn.execute("SELECT route_code FROM routes").fetchall()]
    edges, scouts = get_terminus_stops(conn)
    middles = get_middle_stops(conn) if ENABLE_MIDDLE else []
    conn.close()

    if not route_codes:
        log.error("No routes in DB. Run first_time_setup first.")
        sys.exit(1)

    stop_meta = build_stop_meta(edges + scouts + middles)
    edge_codes = sorted({s["stop_code"] for s in edges})
    middle_codes = sorted({s["stop_code"] for s in middles}) if ENABLE_MIDDLE else []
    cycle_stops = edge_codes + middle_codes   # round-robin set
    scout_codes = sorted({s["stop_code"] for s in scouts} - set(cycle_stops))

    interval = len(cycle_stops) / max(1, TARGET_RATE)   # emergent per-stop interval
    log.info("Two-speed poller: %d routes | %d edge + %d middle stops | "
             "%d scout (1 πέρασμα στα %d) | rate cap %d/s → κάθε στάση ~κάθε %.0fs",
             len(route_codes), len(edge_codes), len(middle_codes),
             len(scout_codes), SCOUT_EVERY, TARGET_RATE, interval)
    if interval > DISAPPEAR_GUARD_MINS * 60:
        log.warning("Stops poll every ~%.0fs > %dmin guard → passages may be missed. "
                    "Raise TARGET_RATE or lower EDGE_DEPTH.", interval, DISAPPEAR_GUARD_MINS)

    # Small bounded queue → feeder self-paces to worker throughput (no backup)
    work_q = queue.Queue(maxsize=STOP_WORKERS * 4)
    result_q = queue.Queue(maxsize=50000)
    limiter = RateLimiter(TARGET_RATE)               # full budget goes to stops now
    stats = {"passages": 0, "pings": 0, "skipped": 0}
    stop_event = threading.Event()

    for _ in range(STOP_WORKERS):
        threading.Thread(target=_stop_worker, args=(work_q, result_q, limiter, stop_event),
                         daemon=True).start()
    threading.Thread(target=_feeder,
                     args=(cycle_stops, work_q, stop_event, scout_codes),
                     daemon=True).start()
    writer = threading.Thread(target=_writer_thread,
                              args=(result_q, stop_meta, stats, stop_event), daemon=True)
    writer.start()

    last_log = time.time()

    try:
        last_job_run = 0.0
        while True:
            if time.time() - last_log >= LOG_EVERY_SECS:
                log.info("two-speed: passages=%d  queue(stop=%d result=%d)",
                         stats["passages"], work_q.qsize(), result_q.qsize())
                # job_runs: αραιά (κάθε 15′) — 1 εγγραφή/λεπτό πνίγει το Pipeline
                if time.time() - last_job_run >= JOB_RUN_EVERY_SECS:
                    try:
                        with db.job_run("local_poll") as run:
                            run.detail = (f"passages={stats['passages']} qlag={work_q.qsize()}")
                    except Exception:
                        pass
                    last_job_run = time.time()
                last_log = time.time()
            time.sleep(0.1)
    except KeyboardInterrupt:
        log.info("Stopping (Ctrl-C). Flushing…")
        stop_event.set()
        writer.join(timeout=10)


if __name__ == "__main__":
    main()
