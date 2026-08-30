-- ==========================================================
-- TUPC LEARNING COMMONS - DATABASE SCHEMA
-- Run this in phpMyAdmin (or the MySQL command line) to
-- create the database and table used by the Flask app.
-- ==========================================================

CREATE DATABASE IF NOT EXISTS learning_commons_db;
USE learning_commons_db;

CREATE TABLE IF NOT EXISTS appointments (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    id_number     VARCHAR(30)  NOT NULL UNIQUE,   -- e.g. LC-07-06-2026-0001
    first_name    VARCHAR(100) NOT NULL,
    last_name     VARCHAR(100) NOT NULL,
    mi            VARCHAR(5),
    gsfe_email    VARCHAR(150) NOT NULL,
    date          DATE         NOT NULL,
    station_no    VARCHAR(50)  NOT NULL,
    location      VARCHAR(100) NOT NULL,
    start_time    TIME         NOT NULL,
    end_time      TIME         NOT NULL,
    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- Helpful index for the daily id_number lookup used by the app
CREATE INDEX idx_id_number ON appointments (id_number);

ALTER TABLE appointments
  ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'reserved';

-- Group Reservation support (this kiosk app's own submit_group_appointment()
-- writes to these columns). first_name/last_name are relaxed to NULL
-- because a group booking only collects one "group representative"
-- name, not per-station first/last names. The admin system's
-- ensure_schema() also applies this automatically on startup - this is
-- mainly here for a fresh manual install via phpMyAdmin, or if you're
-- only ever running the kiosk system against this database.
ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS reservation_type VARCHAR(20) NOT NULL DEFAULT 'individual',
  ADD COLUMN IF NOT EXISTS group_id VARCHAR(30) NULL,
  ADD COLUMN IF NOT EXISTS group_representative VARCHAR(150) NULL,
  ADD COLUMN IF NOT EXISTS purpose VARCHAR(255) NULL,
  MODIFY first_name VARCHAR(100) NULL,
  MODIFY last_name VARCHAR(100) NULL;

ALTER TABLE appointments ADD INDEX IF NOT EXISTS idx_group_id (group_id);

-- ==========================================================
-- STATIONS
-- ==========================================================
-- One row per (location, station_no) pair. This table is now the
-- single source of truth for:
--   - the "Location" dropdowns (DISTINCT location, filtered to
--     is_closed = 0 - see get_active_locations() in app.py)
--   - the station list shown/selectable for a given location (every
--     row for that location - see get_stations_for_location())
--   - each station's "closed" status, independent of whether it has
--     a booking (is_closed = 1 always shows as "closed")
CREATE TABLE IF NOT EXISTS stations (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    location    VARCHAR(100) NOT NULL,
    station_no  VARCHAR(50)  NOT NULL,
    is_closed   TINYINT(1)   NOT NULL DEFAULT 0,
    UNIQUE KEY uq_location_station (location, station_no)
);

CREATE INDEX IF NOT EXISTS idx_stations_location ON stations (location);

-- Seed the original 2 locations x 10 stations so a fresh install
-- matches the app's previous hardcoded LOCATION_LIST/STATION_LIST.
-- INSERT IGNORE means this is safe to re-run without duplicating rows.
INSERT IGNORE INTO stations (location, station_no, is_closed)
SELECT loc.location, CONCAT('Station ', num.n), 0
FROM (
    SELECT 'Main Learning Commons' AS location
    UNION ALL SELECT 'Learning Commons 2'
) AS loc
CROSS JOIN (
    SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5
    UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10
) AS num;

-- ==========================================================
-- EMAIL RESTRICTIONS
-- ==========================================================
-- Any email listed here is blocked from creating a new
-- reservation (individual or group) at the Learning Commons.
-- The app checks this table (case-insensitively) in
-- submit_appointment() / submit_group_appointment() before
-- inserting a new booking.
CREATE TABLE IF NOT EXISTS restrictions (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    gsfe_email     VARCHAR(150) NOT NULL UNIQUE,   -- the restricted GSFE/Gmail address
    reason         VARCHAR(255) NULL,              -- optional note shown to admins (e.g. "Multiple no-shows")
    restricted_by  VARCHAR(150) NULL,              -- optional: admin who added the restriction
    restricted_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_restrictions_gsfe_email ON restrictions (gsfe_email);