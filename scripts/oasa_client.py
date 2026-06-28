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

import requests

BASE_URL        = "https://telematics.oasa.gr/api/"
DEFAULT_TIMEOUT = 12       # seconds
MAX_RETRIES     = 4
BACKOFF_BASE    = 2.0
NO_RETRY_STATUS = {403, 429}   # rate-limit / forbidden — retrying only makes it worse

log = logging.getLogger("oasa_client")


class OasaApiError(Exception):
    pass


def _request(act: str, params: dict[str, str] | None = None,
             timeout: int = DEFAULT_TIMEOUT) -> Any:
    """Single request with retries. Returns parsed JSON or raises OasaApiError."""
    query = {"act": act, **(params or {})}
    last_err: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(BASE_URL, params=query, timeout=timeout)
            # Rate-limit / forbidden: do NOT retry — back off this stop entirely
            if resp.status_code in NO_RETRY_STATUS:
                raise OasaApiError(f"act={act} rate-limited ({resp.status_code})")
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                raise OasaApiError(f"empty response for act={act}")
            return json.loads(text)
        except OasaApiError as e:
            # rate-limit errors are non-retryable; fail fast
            if "rate-limited" in str(e):
                raise
            last_err = e
            if attempt < MAX_RETRIES:
                sleep_s = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                time.sleep(sleep_s)
        except (requests.RequestException, json.JSONDecodeError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                sleep_s = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log.warning("act=%s attempt=%d/%d failed (%s); retrying in %.1fs",
                            act, attempt, MAX_RETRIES, e, sleep_s)
                time.sleep(sleep_s)

    raise OasaApiError(
        f"act={act} params={params} failed after {MAX_RETRIES} attempts: {last_err}"
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
    return _request("getBusLocation", {"p1": route_code})

def get_stop_arrivals(stop_code: str) -> list[dict]:
    """
    Returns predicted arrivals at a stop.
    Each entry has: route_code, vehicle_no, btime2 (mins until arrival),
    route_descr, etc.
    """
    result = _request("getStopArrivals", {"p1": stop_code})
    if result is None:
        return []
    return result if isinstance(result, list) else []


# ── Batch helpers ─────────────────────────────────────────────────────────────

@dataclass
class BatchResult:
    ok:     dict[str, Any] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def success_count(self) -> int: return len(self.ok)
    @property
    def failure_count(self) -> int: return len(self.failed)


def batch_get_bus_locations(route_codes: list[str],
                            max_workers: int = 16) -> BatchResult:
    result = BatchResult()

    def fetch_one(code: str):
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
