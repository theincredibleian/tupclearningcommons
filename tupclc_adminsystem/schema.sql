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