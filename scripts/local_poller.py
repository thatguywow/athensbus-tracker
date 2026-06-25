"""
local_poller.py — runs continuously on your LOCAL machine.

Replaces the GitHub Actions poll-live.yml cron (which can't reach OASA).
Every 5 minutes:
  1. getBusLocation for all routes → pings.jsonl artifact
  2. getStopArrivals for all terminus stops → terminus.jsonl artifact
  3. Uploads both to GitHub as Actions artifacts via the GitHub API

Usage:
    python scripts/local_poller.py

Required environment variables (set once in your shell or a .env file):
    GITHUB_TOKEN   — a personal access token with repo + workflow scope
    GITHUB_REPO    — e.g. "yourname/athensbus-tracker"

The script runs forever. Stop it with Ctrl+C.
On Windows you can run it as a scheduled task or just leave a terminal open.
On Linux/Mac add it to cron: @reboot cd /path/to/project && python scripts/local_poller.py
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests as req

sys.path.insert(0, str(Path(__file__).parent))
import db
import oasa_client as oasa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("local_poller.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("local_poller")

POLL_INTERVAL_SECS = 300   # 5 minutes
MAX_WORKERS        = 16


# ── GitHub artifact upload ────────────────────────────────────────────────────

def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def upload_artifact(name: str, filename: str, content: bytes,
                    token: str, repo: str, retention_days: int = 2) -> bool:
    """
    Upload a file as a GitHub Actions artifact using the Artifact API.
    Creates a workflow run artifact via the REST API.
    Returns True on success.
    """
    # We use the artifact upload endpoint that doesn't require an active
    # workflow run: POST /repos/{owner}/{repo}/actions/artifacts
    # This is the v4 artifact format.
    try:
        # Step 1: create artifact container
        url = f"https://api.github.com/repos/{repo}/actions/artifacts"
        # GitHub's artifact API requires uploading via a workflow run.
        # Since we're not in a workflow, we use the upload URL approach:
        # create a "fake" run artifact by posting to the artifact backend.
        # The supported approach for external uploads is the Actions artifact
        # service — we use the public upload URL pattern.

        # Simpler: write to a temp file and commit directly as a data file,
        # letting the daily workflow pick it up. This avoids needing workflow
        # run context entirely.
        # We commit ping batches as small files to a dedicated branch.
        return _commit_artifact(name, filename, content, token, repo)
    except Exception as e:
        log.error("upload_artifact failed: %s", e)
        return False


def _commit_artifact(name: str, filename: str, content: bytes,
                     token: str, repo: str) -> bool:
    """
    Commit a ping batch file to the 'ping-artifacts' branch of the repo.
    The daily workflow checks out this branch to collect ping files.
    Much simpler than the Actions artifact API which requires workflow context.
    """
    import base64

    headers = _github_headers(token)
    base = f"https://api.github.com/repos/{repo}"

    # Ensure the ping-artifacts branch exists
    try:
        r = req.get(f"{base}/git/ref/heads/ping-artifacts", headers=headers, timeout=15)
        if r.status_code == 404:
            # Get main branch SHA to branch from
            main = req.get(f"{base}/git/ref/heads/main", headers=headers, timeout=15)
            main.raise_for_status()
            sha = main.json()["object"]["sha"]
            req.post(f"{base}/git/refs", headers=headers, timeout=15, json={
                "ref": "refs/heads/ping-artifacts",
                "sha": sha,
            })
    except Exception as e:
        log.warning("Branch check failed: %s", e)

    # Upload the file
    path = f"artifacts/{name}/{filename}"
    encoded = base64.b64encode(content).decode()

    # Check if file already exists (need its SHA to update)
    existing_sha = None
    try:
        ex = req.get(f"{base}/contents/{path}?ref=ping-artifacts",
                     headers=headers, timeout=15)
        if ex.status_code == 200:
            existing_sha = ex.json()["sha"]
    except Exception:
        pass

    payload: dict = {
        "message": f"ping batch: {name}",
        "content": encoded,
        "branch": "ping-artifacts",
    }
    if existing_sha:
        payload["sha"] = existing_sha

    r = req.put(f"{base}/contents/{path}", headers=headers,
                json=payload, timeout=30)
    if r.status_code in (200, 201):
        log.debug("Uploaded %s/%s to ping-artifacts branch", name, filename)
        return True
    else:
        log.error("Upload failed %s: %s %s", path, r.status_code, r.text[:200])
        return False


# ── Poll functions ────────────────────────────────────────────────────────────

def collect_pings(route_codes: list[str], polled_at: str) -> tuple[list[dict], dict]:
    batch = oasa.batch_get_bus_locations(route_codes, max_workers=MAX_WORKERS)
    pings = []
    parse_errors = 0
    for route_code, vehicles in batch.ok.items():
        for v in (vehicles or []):
            try:
                pings.append({
                    "route_code": route_code,
                    "vehicle_no": str(v["VEH_NO"]),
                    "lat":        float(v["CS_LAT"]),
                    "lng":        float(v["CS_LNG"]),
                    "ts_utc":     oasa.parse_oasa_date(v["CS_DATE"]),
                    "polled_at":  polled_at,
                })
            except (KeyError, ValueError, TypeError):
                parse_errors += 1
    return pings, {
        "routes_ok": batch.success_count,
        "routes_failed": batch.failure_count,
        "pings": len(pings),
        "parse_errors": parse_errors,
    }


def collect_terminus_observations(terminus_stops: list[dict],
                                  polled_at: str) -> list[dict]:
    """
    terminus_stops: list of {'route_code', 'stop_code', 'stop_type'}
    Polls getStopArrivals for each and records predictions.
    """
    stop_codes = list({s["stop_code"] for s in terminus_stops})
    batch = oasa.batch_get_stop_arrivals(stop_codes, max_workers=MAX_WORKERS)

    obs = []
    for stop in terminus_stops:
        arrivals = batch.ok.get(stop["stop_code"], [])
        for a in (arrivals or []):
            try:
                obs.append({
                    "route_code":     stop["route_code"],
                    "stop_code":      stop["stop_code"],
                    "stop_type":      stop["stop_type"],
                    "vehicle_no":     str(a.get("VEH_NO") or a.get("veh_no") or ""),
                    "predicted_mins": int(a.get("btime2") or a.get("time2") or 0),
                    "observed_at":    polled_at,
                })
            except (KeyError, ValueError, TypeError):
                pass
    return obs


def get_terminus_stops(conn) -> list[dict]:
    """
    Returns first and last stop for every route that has stop data.
    Cached — stop definitions change rarely.
    """
    rows = conn.execute("""
        SELECT route_code,
               MIN(stop_order) AS first_order,
               MAX(stop_order) AS last_order
        FROM stops
        GROUP BY route_code
    """).fetchall()

    terminus_stops = []
    for r in rows:
        for order, stype in [(r["first_order"], "origin"),
                              (r["last_order"], "terminus")]:
            sc = conn.execute(
                "SELECT stop_code FROM stops WHERE route_code=? AND stop_order=?",
                (r["route_code"], order)
            ).fetchone()
            if sc:
                terminus_stops.append({
                    "route_code": r["route_code"],
                    "stop_code":  sc["stop_code"],
                    "stop_type":  stype,
                })
    return terminus_stops


def jsonl_bytes(records: list[dict]) -> bytes:
    lines = [json.dumps(r, separators=(",", ":")) for r in records]
    return "\n".join(lines).encode("utf-8")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPO", "")

    if not token or not repo:
        print("ERROR: Set GITHUB_TOKEN and GITHUB_REPO environment variables.")
        print("  export GITHUB_TOKEN=ghp_yourtoken")
        print("  export GITHUB_REPO=yourname/athensbus-tracker")
        sys.exit(1)

    db.ensure_schema()
    log.info("Local poller started. Polling every %ds. Repo: %s", POLL_INTERVAL_SECS, repo)

    conn = db.get_connection()
    route_codes    = [r["route_code"] for r in conn.execute("SELECT route_code FROM routes").fetchall()]
    terminus_stops = get_terminus_stops(conn)
    conn.close()

    if not route_codes:
        log.error("No routes in DB. Run sync_master_data.py first.")
        sys.exit(1)

    log.info("Loaded %d routes, %d terminus stops.", len(route_codes), len(terminus_stops))

    while True:
        cycle_start = time.time()
        polled_at   = oasa.now_utc_iso()
        stamp       = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")

        try:
            # --- Vehicle positions ---
            log.info("Polling vehicle positions...")
            pings, ping_stats = collect_pings(route_codes, polled_at)
            log.info("Pings: %s", ping_stats)

            if pings:
                upload_artifact(
                    f"pings-{stamp}", "pings.jsonl",
                    jsonl_bytes(pings), token, repo
                )

            # --- Terminus stop arrivals ---
            if terminus_stops:
                log.info("Polling terminus arrivals (%d stops)...", len(terminus_stops))
                obs = collect_terminus_observations(terminus_stops, polled_at)
                log.info("Terminus observations: %d", len(obs))
                if obs:
                    upload_artifact(
                        f"terminus-{stamp}", "terminus.jsonl",
                        jsonl_bytes(obs), token, repo
                    )

            # Record successful poll in local DB for pipeline health view
            with db.job_run("local_poll") as run:
                run.detail = (
                    f"routes_ok={ping_stats['routes_ok']} "
                    f"pings={ping_stats['pings']} "
                    f"terminus_obs={len(obs) if terminus_stops else 0}"
                )

        except Exception as e:
            log.error("Poll cycle error: %s", e, exc_info=True)

        # Reload route list every hour in case master data was synced
        if int(time.time()) % 3600 < POLL_INTERVAL_SECS:
            conn = db.get_connection()
            route_codes    = [r["route_code"] for r in
                              conn.execute("SELECT route_code FROM routes").fetchall()]
            terminus_stops = get_terminus_stops(conn)
            conn.close()
            log.info("Reloaded: %d routes, %d terminus stops", len(route_codes), len(terminus_stops))

        elapsed = time.time() - cycle_start
        sleep   = max(0, POLL_INTERVAL_SECS - elapsed)
        log.info("Cycle done in %.1fs. Sleeping %.1fs.", elapsed, sleep)
        time.sleep(sleep)


if __name__ == "__main__":
    main()
