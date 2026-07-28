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


TERMINAL_EDGE_DEPTH = 2      # πόσες ακραίες στάσεις/διαδρομή ελέγχουμε για τερματικό
TERMINAL_PACE_SECS  = 0.15   # ρυθμός κλήσεων: ο poller τρέχει ήδη στα 55/s, οπότε
                             # χωρίς pacing οι κλήσεις των τερματικών προκάλεσαν
                             # μαζικά 403 (952/1400 πέρασαν). Με 0.15s το sync
                             # παίρνει ~3′ και συνυπάρχει ήρεμα με τον poller.


def sync_terminals(conn, synced_at: str) -> int:
    """
    Store each edge stop's TERMINAL identity (OASA `isTerminal`).

    Two routes whose ends share the same terminal_id end at the same physical
    place — the arrival of one is the departure event of the other. That single
    fact drives the loop/sibling handling in the reconstruction, replacing a
    300 m distance guess with the operator's own answer.

    Only the first/last TERMINAL_EDGE_DEPTH stops of each route are queried
    (~1.4k stops instead of 31k), and only those not already known, so a repeat
    run costs almost nothing. Runs as part of master sync ⇒ new or changed
    routes pick up their terminals automatically.
    """
    wanted = {r["stop_code"] for r in conn.execute(f"""
        WITH b AS (SELECT route_code, MIN(stop_order) lo, MAX(stop_order) hi
                   FROM stops GROUP BY route_code)
        SELECT DISTINCT s.stop_code FROM stops s JOIN b ON b.route_code=s.route_code
        WHERE s.stop_order < b.lo + {TERMINAL_EDGE_DEPTH}
           OR s.stop_order > b.hi - {TERMINAL_EDGE_DEPTH}
    """)}
    known = {r["stop_code"] for r in conn.execute(
        "SELECT stop_code FROM stop_terminals")}
    todo = sorted(wanted - known)
    if not todo:
        log.info("Terminals: %d already known, nothing to fetch", len(known))
        return 0

    log.info("Terminals: fetching %d edge stops (%d already known)",
             len(todo), len(known))
    n = 0
    for i, sc in enumerate(todo, 1):
        time.sleep(TERMINAL_PACE_SECS)
        try:
            rows = oasa._request("getStopNameAndXY", {"p1": sc},
                                 attempts=3, retry_forbidden=True) or []
        except Exception:
            rows = []          # δεν αποθηκεύεται ⇒ ξαναδοκιμάζεται στο επόμενο sync
        if not (isinstance(rows, list) and rows and isinstance(rows[0], dict)):
            continue           # αποτυχία/κενό ⇒ καμία εγγραφή, retry αργότερα
        tid = rows[0].get("isTerminal")
        conn.execute("""
            INSERT INTO stop_terminals (stop_code, terminal_id, last_synced)
            VALUES (?,?,?)
            ON CONFLICT(stop_code) DO UPDATE SET
                terminal_id=excluded.terminal_id, last_synced=excluded.last_synced
        """, (sc, str(tid) if tid not in (None, "", "0") else None, synced_at))
        n += 1
        if i % 200 == 0:
            conn.commit()
            log.info("  terminals: %d/%d", i, len(todo))
    conn.commit()
    got = conn.execute("SELECT COUNT(*) FROM stop_terminals "
                       "WHERE terminal_id IS NOT NULL").fetchone()[0]
    log.info("Terminals: %d stops stored, %d marked as terminal", n, got)
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

            try:
                sync_terminals(conn, synced_at)
            except Exception as e:
                log.warning("Terminal sync failed (non-fatal): %s", e)

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
