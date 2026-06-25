"""
generate_site_data.py — bakes JSON files for the GitHub Pages dashboard.
All labels in Greek for the live audience.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import db

OUT_DIR      = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
HISTORY_DAYS = 90


def write_json(name: str, payload):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  {path} ({os.path.getsize(path):,} bytes)")


def main():
    conn   = db.get_connection()
    cutoff = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()

    latest = conn.execute(
        "SELECT MAX(service_date) d FROM daily_route_stats"
    ).fetchone()["d"]

    # ── summary.json ──────────────────────────────────────────────────────────
    routes_latest = []
    sys_actual = sys_sched = 0

    if latest:
        for r in conn.execute("""
            SELECT drs.route_code, r.line_code, r.descr AS route_name,
                   r.route_type, drs.actual_trip_count, drs.scheduled_trip_count,
                   drs.completion_pct, drs.distinct_vehicles,
                   drs.avg_deviation_mins, drs.slot_count
            FROM daily_route_stats drs
            LEFT JOIN routes r ON r.route_code = drs.route_code
            WHERE drs.service_date = ?
            ORDER BY r.line_code, r.route_type
        """, (latest,)).fetchall():
            routes_latest.append({
                "route_code":    r["route_code"],
                "line_code":     r["line_code"],
                "route_name":    r["route_name"],
                "direction":     "Εξερχόμενη" if r["route_type"] == "1"
                                 else "Εισερχόμενη" if r["route_type"] == "2"
                                 else (r["route_type"] or ""),
                "actual":        r["actual_trip_count"],
                "scheduled":     r["scheduled_trip_count"],
                "completion_pct": r["completion_pct"],
                "vehicles":      r["distinct_vehicles"],
                "avg_deviation": r["avg_deviation_mins"],
                "slot_count":    r["slot_count"],
            })
            sys_actual += r["actual_trip_count"] or 0
            sys_sched  += r["scheduled_trip_count"] or 0

    write_json("summary.json", {
        "latest_date":             latest,
        "generated_at":            db.now_utc_iso(),
        "system_actual_trips":     sys_actual,
        "system_scheduled_trips":  sys_sched,
        "system_completion_pct":   round(sys_actual/sys_sched*100, 1) if sys_sched else None,
        "route_count":             len(routes_latest),
        "routes":                  routes_latest,
    })

    # ── history.json ──────────────────────────────────────────────────────────
    hist: dict[str, list] = {}
    for r in conn.execute("""
        SELECT drs.service_date, drs.route_code,
               drs.actual_trip_count, drs.scheduled_trip_count,
               drs.completion_pct, drs.avg_deviation_mins
        FROM daily_route_stats drs
        WHERE drs.service_date >= ?
        ORDER BY drs.service_date
    """, (cutoff,)).fetchall():
        hist.setdefault(r["route_code"], []).append({
            "date":           r["service_date"],
            "actual":         r["actual_trip_count"],
            "scheduled":      r["scheduled_trip_count"],
            "completion_pct": r["completion_pct"],
            "avg_deviation":  r["avg_deviation_mins"],
        })
    write_json("history.json", {
        "since": cutoff, "generated_at": db.now_utc_iso(), "by_route": hist
    })

    # ── vehicle_activity.json — latest day ───────────────────────────────────
    va_rows = []
    if latest:
        for r in conn.execute("""
            SELECT va.vehicle_no, va.route_code, r.line_code,
                   r.descr AS route_name, r.route_type,
                   va.slot_number, va.trip_count,
                   va.first_departure, va.last_departure, va.total_mins
            FROM vehicle_activity va
            LEFT JOIN routes r ON r.route_code = va.route_code
            WHERE va.service_date = ?
            ORDER BY CAST(va.vehicle_no AS INTEGER), va.vehicle_no, va.route_code
        """, (latest,)).fetchall():
            va_rows.append({
                "vehicle_no":      r["vehicle_no"],
                "line_code":       r["line_code"],
                "route_name":      r["route_name"],
                "direction":       "Εξερχόμενη" if r["route_type"] == "1" else "Εισερχόμενη",
                "slot_number":     r["slot_number"],
                "trip_count":      r["trip_count"],
                "first_departure": r["first_departure"],
                "last_departure":  r["last_departure"],
                "total_mins":      r["total_mins"],
            })
    write_json("vehicle_activity.json", {
        "date": latest, "generated_at": db.now_utc_iso(), "vehicles": va_rows
    })

    # ── handoffs.json — latest day ────────────────────────────────────────────
    hf_rows = []
    if latest:
        for r in conn.execute("""
            SELECT sh.slot_number, sh.outgoing_vehicle, sh.incoming_vehicle,
                   sh.handoff_time, sh.gap_mins,
                   r.line_code, r.descr AS route_name, r.route_type
            FROM slot_handoffs sh
            LEFT JOIN routes r ON r.route_code = sh.route_code
            WHERE sh.service_date = ?
            ORDER BY sh.handoff_time
        """, (latest,)).fetchall():
            hf_rows.append({
                "slot_number":      r["slot_number"],
                "outgoing_vehicle": r["outgoing_vehicle"],
                "incoming_vehicle": r["incoming_vehicle"],
                "handoff_time":     r["handoff_time"],
                "gap_mins":         r["gap_mins"],
                "line_code":        r["line_code"],
                "route_name":       r["route_name"],
            })
    write_json("handoffs.json", {
        "date": latest, "generated_at": db.now_utc_iso(), "handoffs": hf_rows
    })

    # ── lines.json — reference ────────────────────────────────────────────────
    lines_map: dict[str, dict] = {}
    for r in conn.execute("""
        SELECT l.line_code, l.descr AS line_name,
               r.route_code, r.descr AS route_name, r.route_type
        FROM lines l
        LEFT JOIN routes r ON r.line_code = l.line_code
        ORDER BY l.line_code
    """).fetchall():
        entry = lines_map.setdefault(r["line_code"], {
            "line_code": r["line_code"],
            "line_name": r["line_name"],
            "routes": []
        })
        if r["route_code"]:
            entry["routes"].append({
                "route_code": r["route_code"],
                "route_name": r["route_name"],
                "direction":  "Εξερχόμενη" if r["route_type"] == "1" else "Εισερχόμενη",
            })
    write_json("lines.json", {
        "generated_at": db.now_utc_iso(), "lines": list(lines_map.values())
    })

    # ── pipeline_health.json ─────────────────────────────────────────────────
    jobs = conn.execute("""
        SELECT job_name, started_at, finished_at, status, detail
        FROM job_runs ORDER BY started_at DESC LIMIT 50
    """).fetchall()
    write_json("pipeline_health.json", {
        "generated_at": db.now_utc_iso(),
        "recent_runs":  [dict(r) for r in jobs],
    })

    conn.close()
    print("Ολοκληρώθηκε η δημιουργία δεδομένων.")


if __name__ == "__main__":
    main()
