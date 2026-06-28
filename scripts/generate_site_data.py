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
SITE_DAYS    = 3    # days available on the site


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

    # System-wide normal (theoretical) trip count for three-way comparison
    sys_normal = conn.execute("""
        SELECT COUNT(*) c FROM normal_schedule WHERE schedule_date=?
    """, (service_date,)).fetchone()["c"]

    write_json(os.path.join(ddir, "summary.json"), {
        "service_date":            service_date,
        "generated_at":            db.now_utc_iso(),
        "system_actual_trips":     sys_actual,
        "system_scheduled_trips":  sys_sched,
        "system_normal_trips":     sys_normal,
        "system_completion_pct":   round(sys_actual/sys_sched*100,1) if sys_sched else None,
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
               t.started_at, t.terminus_arrived_at
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
            "ended_at":      None,
        })

    # Per-route three-way comparison: Normal / Daily / Executed
    route_comparison = []
    for r in conn.execute("""
        SELECT r.route_code, r.line_code, l.line_id, r.descr AS route_name, r.route_type,
            (SELECT COUNT(*) FROM normal_schedule ns
             WHERE ns.route_code=r.route_code AND ns.schedule_date=?) AS normal_count,
            (SELECT COUNT(DISTINCT st.departure_time) FROM scheduled_trips st
             WHERE st.route_code=r.route_code AND st.schedule_date=?) AS daily_count,
            (SELECT COUNT(*) FROM trips t
             WHERE t.route_code=r.route_code AND t.service_date=?) AS executed_count
        FROM routes r
        LEFT JOIN lines l ON l.line_code=r.line_code
    """, (service_date, service_date, service_date)).fetchall():
        nc, dc, ec = r["normal_count"], r["daily_count"], r["executed_count"]
        if not (nc or dc or ec):
            continue
        route_comparison.append({
            "route_code":   r["route_code"],
            "line_code":    r["line_code"],
            "line_id":      r["line_id"] or r["line_code"],
            "route_name":   r["route_name"],
            "direction":    "Εξερχόμενη" if r["route_type"]=="1" else "Εισερχόμενη",
            "normal":       nc,    # Προβλεπόμενα
            "daily":        dc,    # Ημερήσιος προγραμματισμός
            "executed":     ec,    # Εκτελεσμένα
            "planned_cuts": (nc - dc) if (nc and dc is not None and nc >= dc) else 0,
            "failures":     (dc - ec) if (dc and ec is not None and dc >= ec) else 0,
        })

    write_json(os.path.join(ddir, "schedule_distribution.json"), {
        "date": service_date, "generated_at": db.now_utc_iso(),
        "trips": dist_rows,
        "comparison": route_comparison,
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

    # ── kartelakia (slot schedule) ────────────────────────────────────────────
    # Per route: ordered list of scheduled departures with their STABLE καρτελάκι
    # (ordinal position mod slot_count). Independent of whether a trip ran, so the
    # pattern is consistent day to day. Where a trip executed, prefer its matched
    # slot_assignment; otherwise fall back to the stable ordinal lookup.
    slot_rows = []
    for r in conn.execute("""
        SELECT st.route_code, r.line_code, l.line_id,
               r.descr AS route_name, r.route_type,
               st.departure_time,
               (SELECT slot_number FROM slot_assignments sa
                JOIN trips t ON t.id=sa.trip_id
                WHERE t.route_code=st.route_code AND t.service_date=st.schedule_date
                  AND sa.scheduled_departure=st.departure_time
                LIMIT 1) AS slot_number
        FROM scheduled_trips st
        LEFT JOIN routes r ON r.route_code=st.route_code
        LEFT JOIN lines l ON l.line_code=r.line_code
        WHERE st.schedule_date=?
        GROUP BY st.route_code, st.departure_time
        ORDER BY r.line_code, st.route_code, st.departure_time
    """, (service_date,)).fetchall():
        slot_rows.append({
            "route_code":   r["route_code"],
            "line_code":    r["line_code"],
            "line_id":      r["line_id"] or r["line_code"],
            "route_name":   r["route_name"],
            "direction":    "Εξερχόμενη" if r["route_type"]=="1" else "Εισερχόμενη",
            "scheduled_dep": r["departure_time"],
            "slot_number":  r["slot_number"],
            "slot_label":   f"Καρτελάκι {r['slot_number']}" if r["slot_number"] else "—",
        })

    write_json(os.path.join(ddir, "kartelakia.json"), {
        "date": service_date, "generated_at": db.now_utc_iso(), "slots": slot_rows
    })

    # ── pipeline health (shared, not date-specific) ───────────────────────────
    jobs = conn.execute("""
        SELECT job_name, started_at, finished_at, status, detail
        FROM job_runs ORDER BY started_at DESC LIMIT 50
    """).fetchall()
    write_json(os.path.join(OUT_DIR, "pipeline_health.json"), {
        "generated_at": db.now_utc_iso(),
        "recent_runs":  [dict(r) for r in jobs],
    })

    print(f"  Generated data for {service_date}: "
          f"{len(routes_latest)} routes, {len(va_rows)} vehicle records, "
          f"{len(dist_rows)} schedule entries, {len(slot_rows)} slot entries")


def main():
    conn = db.get_connection()

    # Generate for today and the last SITE_DAYS days
    dates_to_generate = [
        (date.today() - timedelta(days=i)).isoformat()
        for i in range(SITE_DAYS)
    ]

    # Write the available dates list for the date picker
    available = []
    for d in dates_to_generate:
        has_data = conn.execute(
            "SELECT 1 FROM daily_route_stats WHERE service_date=? LIMIT 1", (d,)
        ).fetchone()
        if has_data:
            available.append(d)
            generate_for_date(conn, d)

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
