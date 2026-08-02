"""
generate_site_data.py — generates JSON files for the GitHub Pages dashboard.

Rolling 3-day history: generates dated JSON files under docs/data/YYYY-MM-DD/
and removes any dates older than 3 days. The dashboard uses a date picker
to switch between days, defaulting to today.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, timedelta

import db

OUT_DIR      = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
HISTORY_DAYS = 90   # kept in DB

# Πόσες ημέρες βλέπει ο χρήστης στο site.
#
# Το κόστος είναι ~6,4 MB JSON ανά ημέρα (κυρίως το schedule_distribution).
# 30 ημέρες ≈ 190 MB στον δίσκο — ασήμαντο μπροστά στα 13 GB ελεύθερα.
#
# Το ΠΡΑΓΜΑΤΙΚΟ κόστος δεν ήταν ο δίσκος αλλά ο ΧΡΟΝΟΣ: η generate ξανάγραφε
# ΟΛΕΣ τις ημέρες σε ΚΑΘΕ κύκλο (κάθε 15 λεπτά). Με 30 ημέρες αυτό είναι 10×
# δουλειά σε μονοπύρηνο VPS, ξανά και ξανά, για δεδομένα που δεν αλλάζουν.
#
# Οι παλιές ημέρες ΔΕΝ αλλάζουν: μόλις κλείσει το παράθυρο παράδοσης (04:00-07:00
# της επόμενης) τα στατιστικά είναι σταθερά. Άρα ξαναγράφονται μόνο οι
# τελευταίες FRESH_DAYS· οι υπόλοιπες μένουν όπως είναι και απλώς σερβίρονται.
SITE_DAYS    = int(os.environ.get("ATHENSBUS_SITE_DAYS", "30"))
FRESH_DAYS   = 2    # πόσες ξαναϋπολογίζονται σε κάθε κύκλο


def write_json(path: str, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def day_dir(d: str) -> str:
    return os.path.join(OUT_DIR, d)


def purge_old_site_data():
    """Remove dated folders older than SITE_DAYS."""
    cutoff = (date.today() - timedelta(days=SITE_DAYS)).isoformat()
    if not os.path.isdir(OUT_DIR):
        return
    for entry in os.listdir(OUT_DIR):
        full = os.path.join(OUT_DIR, entry)
        if os.path.isdir(full) and entry < cutoff:
            shutil.rmtree(full)
            print(f"  purged old site data: {entry}")


def generate_for_date(conn, service_date: str):
    """Generate all JSON files for a specific service date."""
    ddir = day_dir(service_date)
    os.makedirs(ddir, exist_ok=True)

    # ── summary ──────────────────────────────────────────────────────────────
    routes_latest = []
    sys_actual = sys_sched = 0

    for r in conn.execute("""
        SELECT drs.route_code, r.line_code, l.line_id, r.descr AS route_name,
               r.route_type, drs.actual_trip_count, drs.scheduled_trip_count,
               drs.completion_pct, drs.distinct_vehicles,
               drs.avg_deviation_mins, drs.slot_count
        FROM daily_route_stats drs
        LEFT JOIN routes r ON r.route_code = drs.route_code
        LEFT JOIN lines l ON l.line_code = r.line_code
        WHERE drs.service_date = ?
        ORDER BY CAST(l.line_id AS INTEGER), l.line_id
    """, (service_date,)).fetchall():
        routes_latest.append({
            "route_code":    r["route_code"],
            "line_code":     r["line_code"],
            "line_id":       r["line_id"] or r["line_code"],
            "route_name":    r["route_name"],
            "direction":     "Εξερχόμενη" if r["route_type"]=="1" else "Εισερχόμενη",
            "actual":        r["actual_trip_count"],
            "scheduled":     r["scheduled_trip_count"],
            "completion_pct": r["completion_pct"],
            "vehicles":      r["distinct_vehicles"],
            "avg_deviation": r["avg_deviation_mins"],
            "slot_count":    r["slot_count"],
        })
        sys_actual += r["actual_trip_count"] or 0
        sys_sched  += r["scheduled_trip_count"] or 0

    # Total DISTINCT vehicles across the whole system for the day
    total_vehicles = conn.execute("""
        SELECT COUNT(DISTINCT vehicle_no) c FROM trips WHERE service_date=?
    """, (service_date,)).fetchone()["c"]


    # Execution % = scheduled slots that actually got a vehicle / all scheduled
    # slots. Counting raw trips against slots breaks past 100% whenever the
    # schedule shrinks mid-day (mirror) or extra/split trips exist; slot
    # coverage is by construction bounded at 100%.
    matched_slots = conn.execute("""
        SELECT COUNT(DISTINCT route_code || '|' || scheduled_departure) c
        FROM slot_assignments
        WHERE service_date=? AND scheduled_departure IS NOT NULL
    """, (service_date,)).fetchone()["c"]
    completion = round(min(100.0, matched_slots/sys_sched*100), 1) if sys_sched else None

    write_json(os.path.join(ddir, "summary.json"), {
        "service_date":            service_date,
        "generated_at":            db.now_utc_iso(),
        "system_actual_trips":     sys_actual,
        "system_scheduled_trips":  sys_sched,
        "system_completion_pct":   completion,
        "route_count":             len(routes_latest),
        "total_vehicles":          total_vehicles,
        "routes":                  routes_latest,
    })

    # ── vehicle activity ──────────────────────────────────────────────────────
    va_rows = []
    for r in conn.execute("""
        SELECT va.vehicle_no, va.route_code, r.line_code, l.line_id,
               r.descr AS route_name, r.route_type,
               va.slot_number, va.trip_count,
               va.first_departure, va.last_departure, va.total_mins
        FROM vehicle_activity va
        LEFT JOIN routes r ON r.route_code = va.route_code
        LEFT JOIN lines l ON l.line_code = r.line_code
        WHERE va.service_date = ?
        ORDER BY CAST(va.vehicle_no AS INTEGER), va.vehicle_no, va.route_code
    """, (service_date,)).fetchall():
        va_rows.append({
            "vehicle_no":      r["vehicle_no"],
            "line_code":       r["line_code"],
            "line_id":         r["line_id"] or r["line_code"],
            "route_name":      r["route_name"],
            "direction":       "Εξερχόμενη" if r["route_type"]=="1" else "Εισερχόμενη",
            "slot_number":     r["slot_number"],
            "slot_label":      r["vehicle_no"] or f"Καρτελάκι {r['slot_number']}",
            "trip_count":      r["trip_count"],
            "first_departure": r["first_departure"],
            "last_departure":  r["last_departure"],
            "total_mins":      r["total_mins"],
        })
    write_json(os.path.join(ddir, "vehicle_activity.json"), {
        "date": service_date, "generated_at": db.now_utc_iso(), "vehicles": va_rows
    })

    # ── schedule distribution ─────────────────────────────────────────────────
    known_vehicles: dict[tuple, str] = {}
    for r in conn.execute("""
        SELECT DISTINCT t.route_code, sa.slot_number, t.vehicle_no
        FROM slot_assignments sa JOIN trips t ON t.id=sa.trip_id
        WHERE t.service_date=? ORDER BY t.started_at
    """, (service_date,)).fetchall():
        key = (r["route_code"], r["slot_number"])
        if key not in known_vehicles:
            known_vehicles[key] = r["vehicle_no"]

    dist_rows = []
    for r in conn.execute("""
        SELECT t.route_code, r.line_code, l.line_id, r.descr AS route_name,
               r.route_type, sa.scheduled_departure, sa.slot_number,
               sa.departure_deviation_mins, t.vehicle_no,
               t.started_at, t.terminus_arrived_at,
               EXISTS (
                   SELECT 1 FROM trip_stop_times x
                   WHERE x.trip_id = t.id
                     AND x.stop_order <= (SELECT (MIN(stop_order)+MAX(stop_order))/2.0
                                          FROM stops s WHERE s.route_code = t.route_code)
               ) AS dep_observed
        FROM trips t
        JOIN slot_assignments sa ON sa.trip_id=t.id
        LEFT JOIN routes r ON r.route_code=t.route_code
        LEFT JOIN lines l ON l.line_code=r.line_code
        WHERE t.service_date=?
        ORDER BY r.line_code, t.route_code, sa.scheduled_departure
    """, (service_date,)).fetchall():
        slot_num = r["slot_number"]
        slot_label = f"Καρτελάκι {slot_num}" if slot_num else "—"
        dist_rows.append({
            "route_code":    r["route_code"],
            "line_code":     r["line_code"],
            "line_id":       r["line_id"] or r["line_code"],
            "route_name":    r["route_name"],
            "direction":     "Εξερχόμενη" if r["route_type"]=="1" else "Εισερχόμενη",
            "scheduled_dep": r["scheduled_departure"],
            "slot_number":   slot_num,
            "slot_label":    slot_label,
            "vehicle_no":    r["vehicle_no"],
            "deviation":     r["departure_deviation_mins"],
            "started_at":    r["started_at"],
            # False ⇒ η αναχώρηση ΔΕΝ παρατηρήθηκε (υπολογισμένη από τη Λήξη):
            # το δρομολόγιο έγινε, αλλά η ώρα είναι εκτίμηση — το UI τη σημαίνει.
            "dep_observed":  bool(r["dep_observed"]),
            "ended_at":      r["terminus_arrived_at"],  # NULL for incomplete trips
        })

    # Add missed scheduled trips
    for r in conn.execute("""
        SELECT st.route_code, r.line_code, l.line_id,
               r.descr AS route_name, r.route_type, st.departure_time
        FROM scheduled_trips st
        LEFT JOIN routes r ON r.route_code=st.route_code
        LEFT JOIN lines l ON l.line_code=r.line_code
        LEFT JOIN (
            SELECT sa.scheduled_departure, t.route_code
            FROM slot_assignments sa JOIN trips t ON t.id=sa.trip_id
            WHERE t.service_date=?
        ) actual ON actual.route_code=st.route_code
                AND actual.scheduled_departure=st.departure_time
        WHERE st.schedule_date=? AND actual.scheduled_departure IS NULL
        GROUP BY st.route_code, st.departure_time
        ORDER BY r.line_code, st.route_code, st.departure_time
    """, (service_date, service_date)).fetchall():
        dist_rows.append({
            "route_code":    r["route_code"],
            "line_code":     r["line_code"],
            "line_id":       r["line_id"] or r["line_code"],
            "route_name":    r["route_name"],
            "direction":     "Εξερχόμενη" if r["route_type"]=="1" else "Εισερχόμενη",
            "scheduled_dep": r["departure_time"],
            "slot_number":   None,
            "slot_label":    "—",
            "vehicle_no":    None,
            "deviation":     None,
            "started_at":    None,
            "dep_observed":  True,
            "ended_at":      None,
        })

    # Full catalogue of every route of every line — including variants that did
    # not run today (rare exceptions with one trip, seasonal or event-only
    # branches). The dropdown is built from THIS, so a variant is always
    # selectable; if it had no service its table is simply empty.
    # ΜΟΝΟ οι διαδρομές που αφορούν ΑΥΤΗ την ημέρα: όσες έχουν πρόγραμμα Ή
    # έτρεξαν. Ο κατάλογος έδειχνε και τις 712 πάντα, οπότε το μενού γέμιζε με
    # παραλλαγές που δεν κυκλοφορούν καθόλου εκείνη τη μέρα (μετρημένο για
    # 2026-08-01: 121 από 712 άσχετες).
    #
    # ΕΝΩΣΗ, όχι τομή — και τα δύο σκέλη χρειάζονται:
    #   • έτρεξε ΧΩΡΙΣ πρόγραμμα (43 διαδρομές): πραγματική κίνηση, πρέπει να φαίνεται
    #   • πρόγραμμα ΧΩΡΙΣ δρομολόγια (77): η αποτυχία είναι η ΠΛΗΡΟΦΟΡΙΑ, δείχνει 0%
    #
    # Το φίλτρο είναι ΑΝΑ ΗΜΕΡΑ και γράφεται μέσα στο JSON της ημέρας, οπότε μια
    # γραμμή που τρέχει μόνο Κυριακή παραμένει ορατή στη σελίδα της Κυριακής
    # ακόμη κι αν τη δει κανείς τη Δευτέρα. Δεν πειράζεται τίποτα αναδρομικά.
    catalogue = [{
        "line_id":    r["line_id"],
        "line_code":  r["line_code"],
        "route_code": r["route_code"],
        "route_name": r["descr"] or r["route_code"],
        "direction":  ("Εξερχόμενη" if (r["descr"] or "").strip().endswith(">")
                       else None),
    } for r in conn.execute("""
        SELECT r.route_code, r.line_code, r.descr, l.line_id
        FROM routes r JOIN lines l ON l.line_code = r.line_code
        WHERE r.route_code IN (
            SELECT route_code FROM scheduled_trips WHERE schedule_date = ?
            UNION
            SELECT route_code FROM trips WHERE service_date = ?)
        ORDER BY l.line_id, r.route_code
    """, (service_date, service_date))]
    # Direction comes from the trips when known, so reuse it where available.
    dir_by_route = {t["route_code"]: t["direction"] for t in dist_rows if t.get("direction")}
    for row in catalogue:
        row["direction"] = dir_by_route.get(row["route_code"]) or "—"

    write_json(os.path.join(ddir, "schedule_distribution.json"), {
        "date": service_date, "generated_at": db.now_utc_iso(),
        "trips": dist_rows,
        "routes": catalogue,
    })

    # ── Depots / vehicle types: which vehicle types ran from each depot today ──
    import vehicle_classification as vc
    veh_rows = conn.execute("""
        SELECT DISTINCT vehicle_no FROM trips WHERE service_date=?
    """, (service_date,)).fetchall()

    depot_map = {}     # depot_name → {type_name → count}
    unknown = []
    for r in veh_rows:
        depot, vtype = vc.classify(r["vehicle_no"])
        if not depot and not vtype:
            unknown.append(r["vehicle_no"]); continue
        dname = depot or "Άγνωστο αμαξοστάσιο"
        tname = vtype or "Άγνωστος τύπος"
        depot_map.setdefault(dname, {}).setdefault(tname, 0)
        depot_map[dname][tname] += 1

    depots_out = []
    for dname, types in depot_map.items():
        type_list = sorted(({"type": t, "count": c} for t, c in types.items()),
                           key=lambda x: -x["count"])
        depots_out.append({
            "depot": dname,
            "total": sum(types.values()),
            "types": type_list,
        })
    # Fixed display order for depots (as requested)
    DEPOT_ORDER = ["Βοτανικός", "Πειραιάς", "Ράλλη", "Μπραχάμι", "Ανθούσα",
                   "Λιόσια", "ΚΤΕΛ", "ΡΟΥΦ", "Κόκκινος Μύλος"]
    def depot_rank(name):
        return DEPOT_ORDER.index(name) if name in DEPOT_ORDER else len(DEPOT_ORDER)
    depots_out.sort(key=lambda x: depot_rank(x["depot"]))

    write_json(os.path.join(ddir, "depots.json"), {
        "date": service_date, "generated_at": db.now_utc_iso(),
        "depots": depots_out,
        "unclassified_count": len(unknown),
    })


    # ── pipeline health (shared, not date-specific) ───────────────────────────
    jobs = conn.execute("""
        SELECT job_name, started_at, finished_at, status, detail
        FROM job_runs ORDER BY started_at DESC LIMIT 50
    """).fetchall()
    # Data-quality audit: per-day counts of impossible results (see audit_day).
    try:
        audit = [dict(r) for r in conn.execute("""
            SELECT service_date, finding_type, count
            FROM audit_summary
            WHERE service_date = ? AND count > 0
            ORDER BY count DESC
        """, (service_date,))]
        examples = [dict(r) for r in conn.execute("""
            SELECT service_date, finding_type, line_id, route_code,
                   vehicle_no, detail
            FROM audit_findings WHERE service_date = ?
            ORDER BY finding_type LIMIT 40
        """, (service_date,))]
    except Exception:
        audit, examples = [], []

    write_json(os.path.join(OUT_DIR, "pipeline_health.json"), {
        "generated_at": db.now_utc_iso(),
        "recent_runs":  [dict(r) for r in jobs],
        "audit":        audit,
        "audit_examples": examples,
    })

    print(f"  Generated data for {service_date}: "
          f"{len(routes_latest)} routes, {len(va_rows)} vehicle records, "
          f"{len(dist_rows)} schedule entries")


def main():
    conn = db.get_connection()

    # Generate for today and the last SITE_DAYS days
    dates_to_generate = [
        (date.today() - timedelta(days=i)).isoformat()
        for i in range(SITE_DAYS)
    ]

    # Write the available dates list for the date picker
    available = []
    regenerated = reused = 0
    for i, d in enumerate(dates_to_generate):
        has_data = conn.execute(
            "SELECT 1 FROM daily_route_stats WHERE service_date=? LIMIT 1", (d,)
        ).fetchone()
        if not has_data:
            continue
        available.append(d)
        # Ημέρα που έχει ήδη γραφτεί και δεν μπορεί πια να αλλάξει: μένει ως έχει.
        if i >= FRESH_DAYS and os.path.exists(
                os.path.join(day_dir(d), "summary.json")):
            reused += 1
            continue
        generate_for_date(conn, d)
        regenerated += 1
    print(f"  {regenerated} ημέρες ξαναγράφτηκαν, {reused} επαναχρησιμοποιήθηκαν")

    write_json(os.path.join(OUT_DIR, "available_dates.json"), {
        "dates": available,
        "latest": available[0] if available else None,
        "generated_at": db.now_utc_iso(),
    })

    purge_old_site_data()
    conn.close()
    print(f"Site data generation complete. Available dates: {available}")


if __name__ == "__main__":
    main()
