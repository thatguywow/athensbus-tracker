"""
sync_master_data.py — weekly job.

Pulls all lines, all their routes, and all stops for each route from OASA,
and upserts them into the lines / routes / stops tables.

This is the slowest-changing data (line/route/stop definitions rarely change),
so weekly is plenty. Run manually any time with: python scripts/sync_master_data.py
"""

from __future__ import annotations

import logging
import sys
import time

import db
import oasa_client as oasa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_master_data")


def upsert_lines(conn, lines: list[dict], synced_at: str) -> int:
    n = 0
    for line in lines:
        # webGetLines field names per OASA docs: LineCode, LineID, LineDescr, LineDescrEng
        line_code = line.get("LineCode") or line.get("line_code")
        if not line_code:
            continue
        conn.execute(
            """
            INSERT INTO lines (line_code, line_id, descr, descr_eng, last_synced)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(line_code) DO UPDATE SET
                line_id = excluded.line_id,
                descr = excluded.descr,
                descr_eng = excluded.descr_eng,
                last_synced = excluded.last_synced
            """,
            (
                str(line_code),
                str(line.get("LineID") or line.get("line_id") or ""),
                line.get("LineDescr") or line.get("line_descr"),
                line.get("LineDescrEng") or line.get("line_descr_eng"),
                synced_at,
            ),
        )
        n += 1
    return n


def upsert_routes(conn, line_code: str, routes: list[dict], synced_at: str) -> list[str]:
    route_codes = []
    for r in routes:
        route_code = r.get("RouteCode")
        if not route_code:
            continue
        conn.execute(
            """
            INSERT INTO routes (route_code, line_code, descr, descr_eng, route_type, distance_m, last_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(route_code) DO UPDATE SET
                line_code = excluded.line_code,
                descr = excluded.descr,
                descr_eng = excluded.descr_eng,
                route_type = excluded.route_type,
                distance_m = excluded.distance_m,
                last_synced = excluded.last_synced
            """,
            (
                str(route_code),
                str(line_code),
                r.get("RouteDescr"),
                r.get("RouteDescrEng"),
                r.get("RouteType"),
                float(r["RouteDistance"]) if r.get("RouteDistance") else None,
                synced_at,
            ),
        )
        route_codes.append(str(route_code))
    return route_codes


def upsert_stops(conn, route_code: str, stops: list[dict], synced_at: str) -> int:
    # Replace wholesale for this route: stop order/membership can change between syncs
    conn.execute("DELETE FROM stops WHERE route_code = ?", (route_code,))
    n = 0
    for s in stops:
        order = s.get("RouteStopOrder")
        if order is None:
            continue
        conn.execute(
            """
            INSERT INTO stops (route_code, stop_order, stop_code, stop_id, descr, descr_eng, lat, lng, last_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route_code,
                int(order),
                str(s.get("StopCode")),
                str(s.get("StopID") or ""),
                s.get("StopDescr"),
                s.get("StopDescrEng"),
                float(s["StopLat"]) if s.get("StopLat") else None,
                float(s["StopLng"]) if s.get("StopLng") else None,
                synced_at,
            ),
        )
        n += 1
    return n


def main():
    db.ensure_schema()
    synced_at = db.now_utc_iso()

    with db.job_run("sync_master_data") as run:
        conn = db.get_connection()
        try:
            log.info("Fetching all lines...")
            lines = oasa.web_get_lines()
            n_lines = upsert_lines(conn, lines, synced_at)
            conn.commit()
            log.info("Upserted %d lines", n_lines)

            line_codes = [
                str(l.get("LineCode") or l.get("line_code"))
                for l in lines
                if l.get("LineCode") or l.get("line_code")
            ]

            total_routes = 0
            total_stops = 0
            failed_lines = []
            failed_routes = []

            for i, line_code in enumerate(line_codes, 1):
                try:
                    routes = oasa.web_get_routes(line_code)
                except Exception as e:
                    log.warning("Failed to fetch routes for line %s: %s", line_code, e)
                    failed_lines.append(line_code)
                    continue

                route_codes = upsert_routes(conn, line_code, routes, synced_at)
                conn.commit()
                total_routes += len(route_codes)

                for route_code in route_codes:
                    try:
                        stops = oasa.web_get_stops(route_code)
                        n_stops = upsert_stops(conn, route_code, stops, synced_at)
                        conn.commit()
                        total_stops += n_stops
                    except Exception as e:
                        log.warning("Failed to fetch stops for route %s: %s", route_code, e)
                        failed_routes.append(route_code)

                if i % 25 == 0:
                    log.info("Progress: %d/%d lines processed", i, len(line_codes))
                    time.sleep(0.2)  # be a little polite to the upstream API

            run.detail = (
                f"lines={n_lines} routes={total_routes} stops={total_stops} "
                f"failed_lines={len(failed_lines)} failed_routes={len(failed_routes)}"
            )
            if failed_lines or failed_routes:
                run.status = "partial"
                log.warning("Failed lines: %s", failed_lines)
                log.warning("Failed routes: %s", failed_routes)
            log.info("Done. %s", run.detail)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
