"""
sync_schedules.py — daily job.

Pulls getDailySchedule per line and stores theoretical departure times.
Strictly filters to valid service hours (04:00-23:59) and clean HH:MM:SS format.
Ignores midnight/invalid entries that OASA sometimes returns.
"""

from __future__ import annotations

import logging
import sqlite3
import time as _time
from datetime import datetime, date, time

import db
import oasa_client as oasa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_schedules")

# Valid service window — anything outside this is an OASA data artifact
SERVICE_START = time(0, 0)        # accept the whole service day…
SERVICE_END   = time(23, 59, 59)  # …including after-midnight night buses (00:00–03:59)


def _dep_key(t_str: str) -> int:
    """Minutes within the service day: hours < 04 (night buses) sort at the END."""
    h, m = int(t_str[:2]), int(t_str[3:5])
    if h < 4:
        h += 24
    return h * 60 + m


def _now_key() -> int:
    """Current Athens time as a service-day key (comparable with _dep_key)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Athens"))
    except Exception:
        now = datetime.now()
    return _dep_key(now.strftime("%H:%M"))


def is_valid_departure(t_str: str) -> bool:
    """Accept only clean HH:MM:SS times within the service window."""
    try:
        t = datetime.strptime(t_str, "%H:%M:%S").time()
        return SERVICE_START <= t <= SERVICE_END
    except ValueError:
        return False


def extract_departure_times(entries: list[dict], direction: str) -> list[tuple[str, str]]:
    """
    Extract (sdd_code, HH:MM:SS) pairs from getDailySchedule entries for ONE
    direction. OASA stores the outbound (go) time in sde_start1 and the inbound
    (come) time in sde_start2 within each entry, so we must read only the field
    matching the direction — otherwise both directions get merged into one list.
    """
    field = "sde_start1" if direction == "go" else "sde_start2"
    out = []
    for e in entries:
        sdd_code = str(e.get("sdd_code") or "")
        raw = e.get(field)
        if not raw:
            continue
        try:
            t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").time()
            t_str = t.strftime("%H:%M:%S")
            if is_valid_departure(t_str):
                out.append((sdd_code, t_str))
        except ValueError:
            continue
    return out


WEEKDAY_TERMS = ["ΔΕΥΤΕΡΑ -", "ΚΑΘΗΜΕΡΙΝΗ", "ΚΑΘΗΜΕΡΙΝH", "ΟΛΕΣ"]


def pick_sdc_code(line_code: str) -> str | None:
    """
    Choose the schedule day-type code (sdc_code) for TODAY from
    getScheduleDaysMasterline: Sunday / Saturday / Friday / weekday.
    Adapted from fragkakis SdcCodePicker.
    """
    try:
        types = oasa.get_schedule_days_masterline(line_code)
    except Exception:
        return None
    if not types:
        return None

    def find(term):
        for e in types:
            if term in str(e.get("sdc_descr") or ""):
                return str(e.get("sdc_code") or "")
        return None

    weekday = None
    for term in WEEKDAY_TERMS:
        weekday = find(term)
        if weekday:
            break

    wd = date.today().weekday()  # Mon=0 .. Sun=6
    if wd == 6:    # Sunday
        return find("ΚΥΡΙΑΚΗ") or weekday
    if wd == 5:    # Saturday
        return find("ΣΑΒΒΑΤΟ") or weekday
    if wd == 4:    # Friday
        return find("ΠΑΡΑΣΚΕΥΗ") or weekday
    return weekday


def sync_normal_schedules(conn, routes_by_line, lines_meta, today, synced_at) -> int:
    """
    Sync the NORMAL (theoretical) timetable via getSchedLines, using the
    day-correct sdc_code. Stores into normal_schedule for the three-way
    comparison. Independent of the daily sync; failures here never affect it.
    """
    total = 0
    for line_code, meta in lines_meta.items():
        line_id = meta["line_id"]
        try:
            sdc_code = pick_sdc_code(line_code)
            if not sdc_code:
                continue
            # Small gap between the 2 back-to-back calls per line: this phase
            # runs alongside the poller, and unpaced bursts trip the IP limit.
            _time.sleep(0.2)
            sched = oasa.get_sched_lines(line_id, sdc_code, line_code)
        except Exception:
            continue
        if not isinstance(sched, dict):
            continue

        routes_for_line = routes_by_line.get(line_code, [])
        come_route = next((r for r in routes_for_line if r["route_type"] == "2"), None)
        go_route   = next((r for r in routes_for_line if r["route_type"] == "1"), None)

        for direction_key, route in (("come", come_route), ("go", go_route)):
            if route is None:
                continue
            entries = sched.get(direction_key) or []
            times = extract_departure_times(entries, direction_key)
            conn.execute(
                "DELETE FROM normal_schedule WHERE route_code=? AND schedule_date=?",
                (route["route_code"], today)
            )
            seen = set()
            for _sdd, dep_time in times:
                if dep_time in seen:
                    continue
                seen.add(dep_time)
                conn.execute("""
                    INSERT INTO normal_schedule
                        (route_code, schedule_date, departure_time, sdc_code, last_synced)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(route_code, schedule_date, departure_time)
                    DO UPDATE SET sdc_code=excluded.sdc_code, last_synced=excluded.last_synced
                """, (route["route_code"], today, dep_time, sdc_code, synced_at))
                total += 1
    return total


def main(include_normal: bool = True):
    db.ensure_schema()
    synced_at = db.now_utc_iso()
    today = date.today().isoformat()

    with db.job_run("sync_schedules") as run:
        conn = db.get_connection()
        try:
            line_rows = conn.execute("SELECT line_code, line_id FROM lines").fetchall()
            line_codes = [r["line_code"] for r in line_rows]
            lines_meta = {r["line_code"]: {"line_id": r["line_id"]} for r in line_rows}
            log.info("Syncing schedules for %d lines", len(line_codes))

            route_rows = conn.execute(
                "SELECT route_code, line_code, route_type FROM routes"
            ).fetchall()
            routes_by_line: dict[str, list] = {}
            for r in route_rows:
                routes_by_line.setdefault(r["line_code"], []).append(r)

            total_inserted = 0
            failed = []

            for i, line_code in enumerate(line_codes, 1):
                try:
                    sched = oasa.get_daily_schedule(line_code)
                except Exception as e:
                    failed.append(line_code)
                    continue

                routes_for_line = routes_by_line.get(line_code, [])
                come_route = next(
                    (r for r in routes_for_line if r["route_type"] == "2"), None
                )
                go_route = next(
                    (r for r in routes_for_line if r["route_type"] == "1"), None
                )

                for direction_key, route in (("come", come_route), ("go", go_route)):
                    if route is None:
                        continue
                    entries = sched.get(direction_key) or []
                    times = extract_departure_times(entries, direction_key)
                    new_times = []
                    seen = set()
                    for sdd_code, dep_time in times:
                        if dep_time in seen:
                            continue   # OASA duplicates (08:25, 08:25)
                        seen.add(dep_time)
                        new_times.append((sdd_code, dep_time))

                    existing = [r["departure_time"] for r in conn.execute(
                        "SELECT departure_time FROM scheduled_trips "
                        "WHERE route_code=? AND schedule_date=?",
                        (route["route_code"], today)).fetchall()]

                    # ── SAFETY NET 1: never wipe a populated day with an empty
                    # feed (transient 403/empty response). Retry next hour.
                    if existing and not new_times:
                        continue

                    # ── SAFETY NET 2: past-agreement check. Executed times don't
                    # get rewritten by OASA, so if most of OUR already-passed
                    # times are missing from the feed, the feed is for another
                    # day (e.g. tomorrow's served before midnight) or garbage —
                    # skip this line, keep what we have. Triggers only on BULK
                    # mismatch (>=3 missing AND <60% overlap): a stationmaster
                    # editing 1-2 recent slots must still mirror through.
                    if existing:
                        nk = _now_key()
                        past = {t for t in existing if _dep_key(t) <= nk}
                        if past:
                            feed_set = {t for _, t in new_times}
                            missing = len(past - feed_set)
                            overlap = 1 - missing / len(past)
                            if missing >= 3 and overlap < 0.6:
                                continue

                    # ── SILENT MIRROR: the day's schedule always reflects
                    # OASA's latest daily feed — additions, moves and removals
                    # alike, past and future. Self-healing: any bad sync is
                    # corrected by the next hourly one.
                    conn.execute(
                        "DELETE FROM scheduled_trips "
                        "WHERE route_code=? AND schedule_date=?",
                        (route["route_code"], today))
                    for sdd_code, dep_time in new_times:
                        conn.execute(
                            """
                            INSERT INTO scheduled_trips
                                (route_code, schedule_date, departure_time,
                                 raw_sdd_code, last_synced)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(route_code, schedule_date,
                                        departure_time, raw_sdd_code)
                            DO UPDATE SET last_synced = excluded.last_synced
                            """,
                            (route["route_code"], today, dep_time,
                             sdd_code, synced_at),
                        )
                        total_inserted += 1

                if i % 50 == 0:
                    conn.commit()
                    log.info("Progress: %d/%d lines", i, len(line_codes))

            conn.commit()

            # Sync the NORMAL (theoretical) timetable for three-way comparison.
            # It changes rarely, so hourly re-syncs skip it (include_normal=False)
            # and only the daily schedule above is refreshed (freeze-merge).
            normal_rows = 0
            if include_normal:
                try:
                    normal_rows = sync_normal_schedules(
                        conn, routes_by_line, lines_meta, today, synced_at)
                    conn.commit()
                except Exception as e:
                    log.warning("Normal schedule sync failed: %s", e)

            run.detail = (
                f"date={today} schedule_rows={total_inserted} "
                f"normal_rows={normal_rows} failed_lines={len(failed)}"
            )
            if failed:
                run.status = "partial"
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
