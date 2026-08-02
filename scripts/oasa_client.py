"""
oasa_client.py — OASA Telematics API client.

All calls go directly to telematics.oasa.gr — this must run on a local
machine (not GitHub Actions) since OASA blocks cloud provider IPs.
Retries with exponential backoff, bounded concurrency for batch calls.
"""

from __future__ import annotations

import json
import time
import random
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import ssl
import threading

import requests

BASE_URL        = "https://telematics.oasa.gr/api/"
DEFAULT_TIMEOUT = 20       # seconds — OASA is slow, 12s was too tight
MAX_RETRIES     = 4
BACKOFF_BASE    = 2.0

log = logging.getLogger("oasa_client")


# ── Connection setup: one session per worker, ONE shared TLS context ────────
# The OASA server answers with `Connection: close`, so every request needs a
# fresh TCP+TLS connection — that part we cannot avoid. What we CAN avoid is
# rebuilding the TLS context each time: profiling the poller on the VPS showed
# `load_verify_locations` burning 115 ms of CPU PER REQUEST (~50% of all CPU),
# because urllib3 re-read the whole CA bundle for every new connection. The CA
# bundle never changes, so it is loaded ONCE here and shared by every
# connection. Certificate verification stays fully enabled — we just stop
# re-parsing the same trust store 25 times a second.
_SSL_CONTEXT = ssl.create_default_context()

_local = threading.local()


class _SharedContextAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that hands urllib3 the pre-built TLS context."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _SSL_CONTEXT
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = _SSL_CONTEXT
        return super().proxy_manager_for(*args, **kwargs)

    def cert_verify(self, conn, url, verify, cert):
        # requests hands urllib3 the CA-bundle PATH for every connection, and
        # urllib3 then calls load_verify_locations() on it — even when a ready
        # context was supplied. That re-parse is the 115 ms/request seen in the
        # profile. Our shared context already holds the system trust store, so
        # clear the per-connection paths: verification is unchanged, the trust
        # store is simply not re-read for each of the 25 requests per second.
        super().cert_verify(conn, url, verify, cert)
        if verify:
            conn.ca_certs = None
            conn.ca_cert_dir = None
            conn.ca_cert_data = None


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Connection": "keep-alive",
        })
        adapter = _SharedContextAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s



class OasaApiError(Exception):
    pass


def _request(act: str, params: dict[str, str] | None = None,
             timeout=DEFAULT_TIMEOUT, retry_forbidden: bool = True,
             attempts: int | None = None) -> Any:
    """
    Single request with retries. Returns parsed JSON or raises OasaApiError.

    retry_forbidden=False → a 403/429 fails immediately (no retry).
    attempts=1 → single shot, no retries at all. Used for the high-volume
    getStopArrivals: the round-robin poller re-polls each stop within ~one
    cycle anyway, so retrying (403 OR timeout) only wastes worker time —
    a dead stop would otherwise hold a worker for ~35s (4 tries + backoff).
    """
    query = {"act": act, **(params or {})}
    last_err: Exception | None = None
    max_tries = attempts if attempts is not None else MAX_RETRIES

    for attempt in range(1, max_tries + 1):
        try:
            # GET is the native method for the telematics API and is far more
            # reliable than POST for getStopArrivals (confirmed empirically + it
            # is what fragkakis uses). A browser User-Agent avoids UA-based blocks.
            resp = _session().get(
                BASE_URL, params=query, timeout=timeout,
            )
            # 404 = no buses/arrivals for this route/stop right now (common at night).
            # Treat as a valid empty result, not an error — and don't retry.
            if resp.status_code == 404:
                return []
            # Rate-limit on a high-volume endpoint: fail fast, don't hammer.
            if resp.status_code in (403, 429) and not retry_forbidden:
                raise OasaApiError(f"act={act} rate-limited ({resp.status_code})")
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                raise OasaApiError(f"empty response for act={act}")
            return json.loads(text)
        except OasaApiError as e:
            if "rate-limited" in str(e):
                raise                       # non-retryable by request
            last_err = e
            if attempt < max_tries:
                sleep_s = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                time.sleep(sleep_s)
        except (requests.RequestException, json.JSONDecodeError) as e:
            last_err = e
            if attempt < max_tries:
                sleep_s = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log.warning("act=%s attempt=%d/%d failed (%s); retrying in %.1fs",
                            act, attempt, max_tries, e, sleep_s)
                time.sleep(sleep_s)

    raise OasaApiError(
        f"act={act} params={params} failed after {max_tries} attempts: {last_err}"
    )


# ── Individual endpoint wrappers ─────────────────────────────────────────────

def web_get_lines() -> list[dict]:
    return _request("webGetLines")

def web_get_routes(line_code: str) -> list[dict]:
    return _request("webGetRoutes", {"p1": line_code})

def web_get_stops(route_code: str) -> list[dict]:
    return _request("webGetStops", {"p1": route_code})

def get_daily_schedule(line_code: str) -> dict:
    return _request("getDailySchedule", {"line_code": line_code})

def get_schedule_days_masterline(line_code: str) -> list[dict]:
    """Returns the available schedule day-types (sdc_code + sdc_descr) for a line."""
    result = _request("getScheduleDaysMasterline", {"p1": line_code})
    return result if isinstance(result, list) else []

def get_sched_lines(line_id: str, sdc_code: str, line_code: str) -> dict:
    """
    Returns the NORMAL (theoretical) timetable for a line on a given day-type.
    line_id is the public line number (e.g. '619'); sdc_code is the day-type
    code from getScheduleDaysMasterline; line_code is the internal code.
    """
    return _request("getSchedLines",
                    {"p1": line_id, "p2": sdc_code, "p3": line_code})

def get_bus_location(route_code: str) -> list[dict]:
    """
    Θέσεις GPS όλων των οχημάτων μιας διαδρομής.

    Πεδία: VEH_NO, CS_DATE, CS_LAT, CS_LNG, ROUTE_CODE και VEH_HEADING
    (η πορεία σε μοίρες — ΔΕΝ είναι στην τεκμηρίωση, αλλά επιστρέφεται πάντα
    και ξεχωρίζει κατεύθυνση σε κοινό τερματικό).

    ΙΔΙΑ ΠΕΙΘΑΡΧΙΑ ΜΕ ΤΟ get_stop_arrivals: attempts=1, χωρίς retry σε 403,
    σύντομο timeout. Ο λόγος είναι μετρημένος — το παλιό GPS μονοπάτι καλούσε
    με τις προεπιλογές (4 προσπάθειες, retry ΚΑΙ στα 403, timeout 20 s) και
    χωρίς κανένα όριο ρυθμού στο batch_get_bus_locations. Ένα 403 γινόταν
    τέσσερα, ο worker κρατιόταν ~35 s, και ο ρυθμός ανέβαινε ακριβώς τη στιγμή
    που ο server ζητούσε να πέσει. Το probe (scripts/probe_rate_limits.py)
    δείχνει ότι στον ΙΔΙΟ ρυθμό τα δύο endpoints έχουν το ίδιο ποσοστό 403 —
    το πρόβλημα ήταν ο τρόπος κλήσης, όχι το endpoint.
    """
    result = _request("getBusLocation", {"p1": route_code},
                      timeout=8, retry_forbidden=False, attempts=1)
    if result is None:
        return []
    return result if isinstance(result, list) else []


def web_get_routes_details_and_stops(route_code: str) -> dict:
    """
    Γεωμετρία διαδρομής + στάσεις σε μία κλήση.

    Επιστρέφει {"details": [{routed_x, routed_y, routed_order}, ...],
                "stops": [...]}. Το `details` είναι η ΠΡΑΓΜΑΤΙΚΗ διαδρομμένη
    πολυγραμμή — όχι ευθείες μεταξύ στάσεων — και είναι ο άξονας πάνω στον
    οποίο προβάλλονται τα στίγματα GPS (βλ. geo.py).
    """
    result = _request("webGetRoutesDetailsAndStops", {"p1": route_code})
    return result if isinstance(result, dict) else {}

def get_stop_arrivals(stop_code: str) -> list[dict]:
    """
    Returns predicted arrivals at a stop.
    Each entry has: route_code, vehicle_no, btime2 (mins until arrival),
    route_descr, etc.
    """
    result = _request("getStopArrivals", {"p1": stop_code},
                      timeout=5, retry_forbidden=False, attempts=1)
    if result is None:
        return []
    return result if isinstance(result, list) else []


# ── Batch helpers ─────────────────────────────────────────────────────────────

class _SimpleLimiter:
    """Token bucket ίδιας λογικής με τον RateLimiter του local_poller."""

    __slots__ = ("rate", "cap", "allowance", "last", "lock")

    def __init__(self, rate: float, burst_cap: float = 5.0):
        self.rate = float(rate)
        # Το πλαφόν ΔΕΝ επιτρέπεται να πέσει κάτω από 1: η acquire() περιμένει
        # allowance >= 1.0, οπότε με cap < 1 το κουπόνι δεν φτάνει ΠΟΤΕ το
        # κατώφλι και ο καλών κολλάει για πάντα. Εμφανίζεται μόνο σε ρυθμούς
        # κάτω από 1 κλήση/δευτ. — δηλαδή ακριβώς στις αραιές, «ευγενικές»
        # ρυθμίσεις που θέλει κανείς σε δοκιμές ή σε μικρό σύνολο διαδρομών.
        self.cap = max(1.0, min(self.rate, burst_cap))
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


@dataclass
class BatchResult:
    ok:     dict[str, Any] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def success_count(self) -> int: return len(self.ok)
    @property
    def failure_count(self) -> int: return len(self.failed)


def batch_get_bus_locations(route_codes: list[str],
                            max_workers: int = 16,
                            rate: float | None = None) -> BatchResult:
    """
    ΠΡΟΣΟΧΗ ΣΤΟΝ ΡΥΘΜΟ: αυτή η συνάρτηση έριχνε παλιά όλες τις διαδρομές στο
    pool χωρίς κανένα φράγμα — 715 κλήσεις όσο γρήγορα προλάβαιναν 16 νήματα,
    δηλαδή κορυφές πολύ πάνω από ό,τι ανέχεται ο ΟΑΣΑ. Μαζί με το τότε
    retry-σε-403 του get_bus_location, αυτό ήταν η γεννήτρια των 403 που
    οδήγησε στην εγκατάλειψη του GPS. Δώσε `rate` (κλήσεις/δευτ.) και τα
    αιτήματα βγαίνουν ομαλά.
    """
    result = BatchResult()
    limiter = _SimpleLimiter(rate) if rate else None

    def fetch_one(code: str):
        if limiter is not None:
            limiter.acquire()
        return code, get_bus_location(code)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, code): code for code in route_codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                _, data = fut.result()
                result.ok[code] = data
            except Exception as e:
                result.failed[code] = str(e)

    return result


def batch_get_stop_arrivals(stop_codes: list[str],
                            max_workers: int = 16) -> BatchResult:
    result = BatchResult()

    def fetch_one(code: str):
        return code, get_stop_arrivals(code)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, code): code for code in stop_codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                _, data = fut.result()
                result.ok[code] = data
            except Exception as e:
                result.failed[code] = str(e)

    return result


# ── Date parsing ──────────────────────────────────────────────────────────────

def parse_oasa_date(raw: str) -> str:
    """
    Parse OASA's CS_DATE format e.g. 'Jun 21 2026 03:15:00:000PM'
    into ISO8601 UTC. OASA timestamps are Europe/Athens local time.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    cleaned = raw.strip()
    dt_naive = datetime.strptime(cleaned, "%b %d %Y %I:%M:%S:%f%p")
    dt_athens = dt_naive.replace(tzinfo=ZoneInfo("Europe/Athens"))
    return dt_athens.astimezone(timezone.utc).isoformat()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
