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

DAILY_PACE_SECS = 0.1   # ρυθμός κλήσεων ημερήσιου προγράμματος (βλ. σχόλιο στη main)

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

# ── Κανονικό (θεωρητικό) πρόγραμμα — «Προβλεπόμενα» ─────────────────────────
# Ο πίνακας normal_schedule, τα get_sched_lines/get_schedule_days_masterline και
# το σχόλιο στο schema.sql υπήρχαν εδώ και καιρό, αλλά τίποτα δεν τα γέμιζε:
# το normal_rows ήταν σταθερά 0. Χωρίς αυτά, ένα δρομολόγιο που λείπει δεν
# ξεχωρίζει σε «κόπηκε από τον σχεδιασμό» και «προγραμματίστηκε αλλά δεν έγινε»
# — δύο εντελώς διαφορετικά πράγματα που μέχρι τώρα μετριούνταν ως ένα.

# Ο ΟΑΣΑ γράφει τους τύπους ημέρας με ΑΝΑΜΕΙΚΤΟ αλφάβητο: «ΘΕΡΙΝΟ ΣΑΒΒΑΤΟY»
# τελειώνει σε λατινικό Y, «ΚΑΘΗΜΕΡΙΝH» σε λατινικό H. Οπτικά ίδια, διαφορετικά
# bytes — μια απλή σύγκριση κειμένου αστοχεί σιωπηλά και διαλέγει λάθος τύπο
# ημέρας, δηλαδή λάθος πρόγραμμα για όλη τη μέρα.
_LOOKALIKE = str.maketrans({
    "A": "Α", "B": "Β", "E": "Ε", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ",
    "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ", "Z": "Ζ",
})


def _norm_greek(s: str) -> str:
    return (s or "").upper().translate(_LOOKALIKE)


def pick_day_type(day_types: list[dict], weekday: int) -> str | None:
    """
    Διαλέγει το sdc_code που ταιριάζει στη σημερινή ημέρα.
    weekday: 0=Δευτέρα … 5=Σάββατο, 6=Κυριακή.
    """
    if not day_types:
        return None
    if weekday == 5:
        want, avoid = "ΣΑΒΒΑΤ", ("ΚΥΡΙΑΚ",)
    elif weekday == 6:
        want, avoid = "ΚΥΡΙΑΚ", ("ΣΑΒΒΑΤ",)
    else:
        want, avoid = "ΚΑΘΗΜΕΡΙΝ", ("ΣΑΒΒΑΤ", "ΚΥΡΙΑΚ")

    for dt in day_types:
        d = _norm_greek(dt.get("sdc_descr", ""))
        if want in d and not any(a in d for a in avoid):
            return str(dt.get("sdc_code") or "")
    # Εφεδρικό: γραμμές με έναν μόνο τύπο («ΟΛΕΣ ΟΙ ΗΜΕΡΕΣ»)
    if len(day_types) == 1:
        return str(day_types[0].get("sdc_code") or "")
    return None


def sync_normal_schedule(conn, service_date: str, synced_at: str,
                         lines_meta: dict, routes_by_line: dict,
                         limiter) -> int:
    """
    Κατεβάζει το ΚΑΝΟΝΙΚΟ πρόγραμμα του τύπου ημέρας και το αποθηκεύει.

    Το κανονικό πρόγραμμα αλλάζει εποχιακά, όχι καθημερινά, οπότε αν υπάρχουν
    ήδη γραμμές για σήμερα δεν ξαναρωτάμε — η δουλειά κοστίζει ~2 κλήσεις ανά
    γραμμή και δεν έχει νόημα να επαναλαμβάνεται κάθε ώρα.
    """
    have = {r["route_code"] for r in conn.execute(
        "SELECT DISTINCT route_code FROM normal_schedule WHERE schedule_date=?",
        (service_date,))}

    weekday = date.fromisoformat(service_date).weekday()
    total = 0
    for line_code, meta in lines_meta.items():
        routes = routes_by_line.get(line_code, [])
        if not routes or all(r["route_code"] in have for r in routes):
            continue
        try:
            limiter.acquire()
            day_types = oasa.get_schedule_days_masterline(line_code)
            sdc = pick_day_type(day_types, weekday)
            if not sdc:
                continue
            limiter.acquire()
            sched = oasa.get_sched_lines(meta["line_id"], sdc, line_code)
        except Exception:
            continue
        if not isinstance(sched, dict):
            continue

        for direction_key, rtype in (("go", "1"), ("come", "2")):
            route = next((r for r in routes if r["route_type"] == rtype), None)
            if route is None:
                continue
            times = {t for _sdd, t in
                     extract_departure_times(sched.get(direction_key) or [],
                                             direction_key)}
            if not times:
                continue
            conn.execute("DELETE FROM normal_schedule WHERE route_code=? "
                         "AND schedule_date=?", (route["route_code"], service_date))
            for t in sorted(times):
                conn.execute("""
                    INSERT INTO normal_schedule
                        (route_code, schedule_date, departure_time, sdc_code,
                         last_synced)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(route_code, schedule_date, departure_time)
                    DO UPDATE SET sdc_code=excluded.sdc_code,
                                  last_synced=excluded.last_synced
                """, (route["route_code"], service_date, t, sdc, synced_at))
                total += 1
        conn.commit()
    return total


def main():
    db.ensure_schema()
    synced_at = db.now_utc_iso()

    # ΗΜΕΡΑ ΒΑΡΔΙΑΣ, όχι ημερολογιακή. Ολόκληρο το υπόλοιπο σύστημα κλειδώνει
    # στο db.athens_service_date() (04:00→04:00): το compute διαβάζει
    # scheduled_trips WHERE schedule_date = service_date, και η _sched_datetimes
    # ερμηνεύει ώρες < 04:00 ως ΤΕΛΟΣ αυτής της ημέρας βάρδιας. Εδώ όμως
    # γραφόταν date.today() — ημερολογιακή. Από τα μεσάνυχτα ως τις 04:00 οι
    # δύο διαφέρουν κατά μία μέρα, οπότε το sync έσβηνε και ξανάγραφε τις
    # γραμμές της ΕΠΟΜΕΝΗΣ ημέρας βάρδιας με ό,τι επέστρεφε ο ΟΑΣΑ εκείνη τη
    # στιγμή, ενώ το compute δούλευε ακόμη την προηγούμενη. Το πεδίο ημερομηνίας
    # του ΟΑΣΑ δεν βοηθά να το λύσουμε αλλιώς: είναι σταθερά '1900-01-01' —
    # καθαρός συμπληρωματικός χαρακτήρας, μόνο η ώρα έχει νόημα.
    today = db.athens_service_date()

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
                # Γραμμή χωρίς διαδρομές στη βάση δεν έχει πού να αποθηκεύσει —
                # η κλήση θα πήγαινε χαμένη. Στην πλήρη εγκατάσταση δεν αλλάζει
                # τίποτα· σε υποσύνολο (τοπική ανάπτυξη) γλιτώνει εκατοντάδες
                # άσκοπες κλήσεις που ανταγωνίζονται τον poller.
                if not routes_by_line.get(line_code):
                    continue
                try:
                    # Pacing: the poller now runs at 55 req/s, so 476 unpaced
                    # schedule calls on top of it pushed us over OASA's limit and
                    # the job kept finishing as `partial` (403s on some lines).
                    # 0.1s per line costs ~45s once an hour and removes the noise.
                    _time.sleep(DAILY_PACE_SECS)
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

            # Κανονικό πρόγραμμα (Προβλεπόμενα) — η τρίτη πλευρά της σύγκρισης
            # «κανονικό vs ημερήσιο vs εκτελεσμένο». Μη μοιραίο αν αποτύχει:
            # το ημερήσιο πρόγραμμα από πάνω είναι που τροφοδοτεί το compute.
            normal_rows = 0
            try:
                normal_rows = sync_normal_schedule(
                    conn, today, synced_at, lines_meta, routes_by_line,
                    oasa._SimpleLimiter(1.0 / DAILY_PACE_SECS))
                conn.commit()
            except Exception as e:
                log.warning("Κανονικό πρόγραμμα απέτυχε (μη μοιραίο): %s", e)

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
