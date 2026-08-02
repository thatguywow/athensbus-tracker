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


def _pick_variant(conn, routes_for_line, route_type: str, target_date: str):
    """
    Ποια παραλλαγή διαδρομής παίρνει το πρόγραμμα αυτής της κατεύθυνσης.

    Πολλές γραμμές έχουν ΠΟΛΛΕΣ διαδρομές ίδιας κατεύθυνσης: κανονική,
    Σαββάτου-Κυριακής, νυχτερινή, «προσωρινή λόγω έργων», κοντή εκδοχή. Ο
    κώδικας έπαιρνε την ΠΡΩΤΗ που τύχαινε (`next(...)`), οπότε το πρόγραμμα
    μπορούσε να προσγειωθεί σε παραλλαγή που δεν κυκλοφορεί σήμερα.

    ΜΕΤΡΗΜΕΝΟ (2026-08-01, Σάββατο): 86 κατευθύνσεις έχουν >1 παραλλαγή· σε 9
    το πρόγραμμα κάθισε σε ΑΛΛΗ παραλλαγή από εκείνη που έτρεχαν τα οχήματα —
    116 προγραμματισμένες αναχωρήσεις σε λάθος διαδρομή. Χαρακτηριστικό
    παράδειγμα η γραμμή 500 (νυχτερινή): πρόγραμμα 9 αναχωρήσεων στη διαδρομή
    1889, ενώ τα λεωφορεία έτρεχαν τη σαββατοκύριακη 2900 με 716 διελεύσεις.
    Αποτέλεσμα: η μία κατεύθυνση δείχνει 0% κάλυψη και η άλλη κανένα πρόγραμμα.

    Επιλογή με ΔΕΔΟΜΕΝΑ: κερδίζει η παραλλαγή με τις περισσότερες διελεύσεις
    τις τελευταίες ημέρες — δηλαδή αυτή που όντως κυκλοφορεί. Αυτοδιορθώνεται
    όταν αλλάζει η εποχή ή τελειώνουν τα έργα. Χωρίς ιστορικό (νέα διαδρομή),
    διατηρείται η παλιά συμπεριφορά.
    """
    cands = [r for r in routes_for_line if r["route_type"] == route_type]
    if len(cands) <= 1:
        return cands[0] if cands else None

    # ΚΡΙΤΗΡΙΟ: ΔΡΟΜΟΛΟΓΙΑ, όχι σκέτες διελεύσεις.
    #
    # Πρώτη εκδοχή μετρούσε διελεύσεις και διάλεγε ΛΑΘΟΣ. Μετρημένο στον VPS,
    # γραμμή 500 κατεύθυνση 2:
    #     1889  διελεύσεις/7ημ = 133  δρομολόγια = 0
    #     2900  διελεύσεις/7ημ =  88  δρομολόγια = 7
    # Το 1889 έχει περισσότερες διελεύσεις αλλά ΜΗΔΕΝ δρομολόγια: σκόρπιες
    # ανιχνεύσεις που δεν συνθέτουν ποτέ διαδρομή. Τα λεωφορεία τρέχουν στο 2900.
    #
    # Ένα ολοκληρωμένο δρομολόγιο είναι πολύ ισχυρότερη ένδειξη «αυτή η
    # παραλλαγή κυκλοφορεί» από μεμονωμένες διελεύσεις, που μπορεί να προέρχονται
    # από κοινές στάσεις με άλλη παραλλαγή. Οι διελεύσεις μένουν ως δεύτερο
    # κριτήριο για νέες διαδρομές που δεν έχουν χτίσει ακόμη δρομολόγια.
    #
    # Δεν υπάρχει κυκλική εξάρτηση: τα δρομολόγια προκύπτουν από διελεύσεις,
    # ποτέ από το πρόγραμμα.
    # ΤΟ ΠΑΡΑΘΥΡΟ ΠΡΕΠΕΙ ΝΑ ΣΥΓΚΡΙΝΕΙ ΟΜΟΙΑ ΜΕ ΟΜΟΙΑ.
    #
    # Επίπεδο παράθυρο 7 ημερών περιέχει 5 καθημερινές και 2 ημέρες
    # σαββατοκύριακου. Άρα μια παραλλαγή ΚΑΘΗΜΕΡΙΝΗΣ σχεδόν πάντα νικά μια
    # παραλλαγή ΣΑΒΒΑΤΟΚΥΡΙΑΚΟΥ — ακόμη και το ίδιο το σαββατοκύριακο, όταν
    # εκείνη τρέχει κι αυτή είναι σταματημένη. Δομική μεροληψία, όχι ατυχία.
    #
    # ΜΕΤΡΗΜΕΝΟ στον VPS, Κυριακή 2026-08-02, γραμμή 500 κατεύθυνση 2:
    #     1889 [ΝΥΧΤΕΡΙΝΗ, καθημερινές]  Δε8 Τρ7 Τε6 Πε6 Πα2  = 32 δρομολόγια
    #     2900 [ΝΥΧΤΕΡΙΝΗ ΣΑΒΒΑΤΟΚΥΡΙΑΚΟ] Σα11 Κυ6 Σα7 Κυ4    = 26 δρομολόγια
    # Κέρδιζε το 1889 με 0 δρομολόγια εκείνη τη μέρα, ενώ το 2900 έτρεχε 6.
    # Το πρόγραμμα πήγαινε στη σταματημένη παραλλαγή και η κατεύθυνση έδειχνε
    # 0% εκτέλεση, με τα πραγματικά δρομολόγια χωρίς πρόγραμμα να ταιριάξουν.
    #
    # Μετράμε λοιπόν ΜΟΝΟ ημέρες ίδιου τύπου με τη μέρα-στόχο: Κυριακή με
    # Κυριακές, Σάββατο με Σάββατα, καθημερινή με καθημερινές. Το παράθυρο
    # ανοίγει σε 28 ημέρες ώστε να υπάρχουν 4 συγκρίσιμες ημέρες ακόμη και για
    # παραλλαγές που τρέχουν μία φορά την εβδομάδα. Το strftime('%w') δίνει
    # 0=Κυριακή, 6=Σάββατο· οι αργίες δεν διακρίνονται, αλλά αυτό είναι
    # ΗΕΥΡΙΣΤΙΚΟ ΕΠΙΛΟΓΗΣ παραλλαγής, όχι το ίδιο το πρόγραμμα.
    dow = conn.execute("SELECT strftime('%w', ?) d", (target_date,)).fetchone()["d"]
    if dow == "0":
        same_day = "strftime('%w', service_date) = '0'"
    elif dow == "6":
        same_day = "strftime('%w', service_date) = '6'"
    else:
        same_day = "strftime('%w', service_date) NOT IN ('0','6')"

    def _score(rc):
        try:
            t = conn.execute(
                f"SELECT COUNT(*) c FROM trips WHERE route_code=? "
                f"AND service_date >= date(?, '-28 day') AND {same_day}",
                (rc, target_date)).fetchone()["c"]
            p = conn.execute(
                f"SELECT COUNT(*) c FROM stop_passages WHERE route_code=? "
                f"AND service_date >= date(?, '-28 day') AND {same_day}",
                (rc, target_date)).fetchone()["c"]
        except Exception:
            return (0, 0)
        return (t, p)

    scored = [(r, _score(r["route_code"])) for r in cands]
    scored.sort(key=lambda x: x[1], reverse=True)
    best, (t, p) = scored[0]
    return best if (t or p) else cands[0]


def main():
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
                come_route = _pick_variant(conn, routes_for_line, "2", today)
                go_route = _pick_variant(conn, routes_for_line, "1", today)

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
                    # Καθαρίζουμε ΟΛΕΣ τις παραλλαγές αυτής της κατεύθυνσης,
                    # όχι μόνο την επιλεγμένη.
                    #
                    # Ο επιλογέας παραλλαγής μπορεί να αλλάξει γνώμη από μέρα σε
                    # μέρα (αλλαγή εποχής, τέλος έργων, Σαββατοκύριακο). Αν
                    # σβήναμε μόνο τη νέα διαδρομή, οι σειρές της ΠΑΛΙΑΣ έμεναν
                    # και το ίδιο πρόγραμμα μετριόταν ΔΥΟ ΦΟΡΕΣ — μία σε κάθε
                    # παραλλαγή. Παρατηρήθηκε στη γραμμή 500: το πρόγραμμα
                    # μετακινήθηκε στη διαδρομή 5909 αλλά η 1890 κράτησε τις
                    # παλιές 5 αναχωρήσεις.
                    same_dir = [r["route_code"] for r in routes_for_line
                                if r["route_type"] == route["route_type"]]
                    conn.execute(
                        "DELETE FROM scheduled_trips WHERE schedule_date=? "
                        "AND route_code IN (%s)" % ",".join("?" * len(same_dir)),
                        [today] + same_dir)
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

            normal_rows = 0

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
