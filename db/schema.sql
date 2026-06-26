-- Athens Bus Tracker — SQLite schema
-- Raw/high-volume tables pruned to rolling windows by compute_daily_report.py
-- Computed/summary tables kept forever (small, ~few KB/day)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─────────────────────── Master data (synced weekly, locally) ───────────────────────

CREATE TABLE IF NOT EXISTS lines (
    line_code   TEXT PRIMARY KEY,
    line_id     TEXT,
    descr       TEXT,
    descr_eng   TEXT,
    last_synced TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_code   TEXT PRIMARY KEY,
    line_code    TEXT NOT NULL REFERENCES lines(line_code),
    descr        TEXT,
    descr_eng    TEXT,
    route_type   TEXT,   -- '1'=outbound, '2'=inbound
    distance_m   REAL,
    last_synced  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routes_line ON routes(line_code);

CREATE TABLE IF NOT EXISTS stops (
    route_code   TEXT NOT NULL REFERENCES routes(route_code),
    stop_order   INTEGER NOT NULL,
    stop_code    TEXT NOT NULL,
    stop_id      TEXT,
    descr        TEXT,
    descr_eng    TEXT,
    lat          REAL,
    lng          REAL,
    last_synced  TEXT NOT NULL,
    PRIMARY KEY (route_code, stop_order)
);
CREATE INDEX IF NOT EXISTS idx_stops_route ON stops(route_code);
CREATE INDEX IF NOT EXISTS idx_stops_code  ON stops(stop_code);

-- ─────────────────────── Schedule data (synced daily, locally) ──────────────────────

CREATE TABLE IF NOT EXISTS scheduled_trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code      TEXT NOT NULL REFERENCES routes(route_code),
    schedule_date   TEXT NOT NULL,
    departure_time  TEXT,           -- HH:MM:SS theoretical departure from origin
    raw_sdd_code    TEXT,
    last_synced     TEXT NOT NULL,
    UNIQUE(route_code, schedule_date, departure_time, raw_sdd_code)
);
CREATE INDEX IF NOT EXISTS idx_sched_route_date ON scheduled_trips(route_code, schedule_date);

-- ─────────────────────── Raw live data (rolling 30-day window) ──────────────────────

CREATE TABLE IF NOT EXISTS vehicle_pings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code  TEXT NOT NULL,
    vehicle_no  TEXT NOT NULL,
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    ts_utc      TEXT NOT NULL,
    polled_at   TEXT NOT NULL,
    UNIQUE(route_code, vehicle_no, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_pings_route_ts   ON vehicle_pings(route_code, ts_utc);
CREATE INDEX IF NOT EXISTS idx_pings_vehicle_ts ON vehicle_pings(vehicle_no, ts_utc);
CREATE INDEX IF NOT EXISTS idx_pings_polled_at  ON vehicle_pings(polled_at);

-- Terminus arrival observations from getStopArrivals (rolling 30-day window)
CREATE TABLE IF NOT EXISTS terminus_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code      TEXT NOT NULL,
    stop_code       TEXT NOT NULL,
    stop_type       TEXT NOT NULL,  -- 'origin' or 'terminus'
    vehicle_no      TEXT,           -- if OASA provides it
    predicted_mins  INTEGER,        -- minutes until arrival as reported by OASA
    observed_at     TEXT NOT NULL,  -- ISO8601 UTC when we polled
    UNIQUE(route_code, stop_code, vehicle_no, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_terminus_route ON terminus_observations(route_code, observed_at);

-- ─────────────────────── Computed trips (kept forever) ──────────────────────────────

CREATE TABLE IF NOT EXISTS trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code      TEXT NOT NULL,
    vehicle_no      TEXT NOT NULL,
    service_date    TEXT NOT NULL,
    started_at      TEXT NOT NULL,      -- ISO8601 UTC, first ping
    ended_at        TEXT NOT NULL,      -- ISO8601 UTC, last ping
    terminus_arrived_at TEXT,           -- from getStopArrivals if available, else interpolated
    stop_count      INTEGER NOT NULL,
    computed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_route_date   ON trips(route_code, service_date);
CREATE INDEX IF NOT EXISTS idx_trips_vehicle_date ON trips(vehicle_no, service_date);

CREATE TABLE IF NOT EXISTS trip_stop_times (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     INTEGER NOT NULL REFERENCES trips(id),
    stop_code   TEXT NOT NULL,
    stop_order  INTEGER NOT NULL,
    passed_at   TEXT NOT NULL,
    distance_m  REAL,
    method      TEXT    -- 'interpolated', 'snapped', 'terminus_observed'
);
CREATE INDEX IF NOT EXISTS idx_tst_trip ON trip_stop_times(trip_id);
CREATE INDEX IF NOT EXISTS idx_tst_stop ON trip_stop_times(stop_code);

CREATE TABLE IF NOT EXISTS vehicle_departures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_no      TEXT NOT NULL,
    route_code      TEXT NOT NULL,
    service_date    TEXT NOT NULL,
    departed_at     TEXT NOT NULL,
    trip_id         INTEGER REFERENCES trips(id),
    computed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vd_date          ON vehicle_departures(service_date);
CREATE INDEX IF NOT EXISTS idx_vd_vehicle_date  ON vehicle_departures(vehicle_no, service_date);

-- ─────────────────────── Rotation slots (kept forever) ──────────────────────────────

-- The inferred rotation pattern for a route: how many slots, what headway
CREATE TABLE IF NOT EXISTS rotation_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code      TEXT NOT NULL,
    service_date    TEXT NOT NULL,
    slot_count      INTEGER NOT NULL,   -- how many buses in rotation
    headway_mins    REAL NOT NULL,      -- average gap between consecutive departures (mins)
    cycle_mins      REAL NOT NULL,      -- full round-trip cycle time (headway * slot_count)
    computed_at     TEXT NOT NULL,
    UNIQUE(route_code, service_date)
);
CREATE INDEX IF NOT EXISTS idx_rp_route_date ON rotation_patterns(route_code, service_date);

-- Each observed trip assigned to a rotation slot
CREATE TABLE IF NOT EXISTS slot_assignments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id             INTEGER NOT NULL REFERENCES trips(id),
    route_code          TEXT NOT NULL,
    service_date        TEXT NOT NULL,
    slot_number         INTEGER NOT NULL,   -- 1-based, slot 1 = first departure of day
    scheduled_departure TEXT,               -- matched theoretical departure time HH:MM:SS
    departure_deviation_mins REAL,          -- actual - scheduled in minutes (+ = late)
    computed_at         TEXT NOT NULL,
    UNIQUE(trip_id)
);
CREATE INDEX IF NOT EXISTS idx_sa_route_date ON slot_assignments(route_code, service_date);
CREATE INDEX IF NOT EXISTS idx_sa_slot       ON slot_assignments(route_code, service_date, slot_number);

-- When a different vehicle takes over a slot mid-day (shift change / break / handoff)
CREATE TABLE IF NOT EXISTS slot_handoffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code      TEXT NOT NULL,
    service_date    TEXT NOT NULL,
    slot_number     INTEGER NOT NULL,
    outgoing_vehicle TEXT NOT NULL,
    incoming_vehicle TEXT NOT NULL,
    handoff_time    TEXT NOT NULL,      -- ISO8601 UTC
    gap_mins        REAL,               -- gap between last outgoing trip and first incoming trip
    computed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sh_route_date ON slot_handoffs(route_code, service_date);

-- Daily summary per vehicle: all routes it ran, all slots it covered
CREATE TABLE IF NOT EXISTS vehicle_activity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_no      TEXT NOT NULL,
    service_date    TEXT NOT NULL,
    route_code      TEXT NOT NULL,
    slot_number     INTEGER,
    trip_count      INTEGER NOT NULL DEFAULT 0,
    first_departure TEXT,           -- ISO8601 UTC
    last_departure  TEXT,           -- ISO8601 UTC
    total_mins      REAL,           -- total time on this route this day
    computed_at     TEXT NOT NULL,
    UNIQUE(vehicle_no, service_date, route_code, slot_number)
);
CREATE INDEX IF NOT EXISTS idx_va_date    ON vehicle_activity(service_date);
CREATE INDEX IF NOT EXISTS idx_va_vehicle ON vehicle_activity(vehicle_no, service_date);

-- ─────────────────────── Daily rollup stats (kept forever) ──────────────────────────

CREATE TABLE IF NOT EXISTS daily_route_stats (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code              TEXT NOT NULL,
    service_date            TEXT NOT NULL,
    actual_trip_count       INTEGER NOT NULL DEFAULT 0,
    scheduled_trip_count    INTEGER NOT NULL DEFAULT 0,
    completion_pct          REAL,
    distinct_vehicles       INTEGER NOT NULL DEFAULT 0,
    avg_deviation_mins      REAL,   -- average lateness across all trips
    slot_count              INTEGER,
    computed_at             TEXT NOT NULL,
    UNIQUE(route_code, service_date)
);
CREATE INDEX IF NOT EXISTS idx_drs_date  ON daily_route_stats(service_date);
CREATE INDEX IF NOT EXISTS idx_drs_route ON daily_route_stats(route_code);

-- Pipeline bookkeeping
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobruns_name_time ON job_runs(job_name, started_at);

-- ─────────────────────── Persistent slot definitions (kept forever) ──────────
-- Stores the stable slot structure per route: how many slots, what cycle.
-- Updated weekly from observed data. Used to label slots consistently
-- across days even before today's vehicle assignments are known.
CREATE TABLE IF NOT EXISTS slot_definitions (
    route_code      TEXT NOT NULL,
    slot_number     INTEGER NOT NULL,
    typical_first_dep TEXT,     -- typical first departure of day for this slot HH:MM
    typical_interval_mins REAL, -- typical interval between this slot's departures
    last_updated    TEXT NOT NULL,
    PRIMARY KEY (route_code, slot_number)
);
