import io
import json
import re
import smtplib
from datetime import datetime, time as time_cls, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import mysql.connector
import qrcode
from flask import Flask, jsonify, redirect, render_template, request, url_for

from db_config import DB_CONFIG, EMAIL_CONFIG

app = Flask(__name__, template_folder="templates")


@app.after_request
def add_no_cache_headers(response):
    """
    Station/location status must always reflect the current row in the
    `stations`/`appointments` tables (is_closed, current bookings, etc.)
    - never a stale copy. Without explicit no-cache headers, some
    browsers and any reverse proxy/CDN sitting in front of this app can
    cache a GET JSON response (e.g. /api/station-status), which looks
    exactly like "the grid still says closed even though the database
    was already updated to open" until the cached copy expires. Applied
    to every /api/... route so this can't happen for any of them.
    """
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# ==========================================================
# STATION / LOCATION CONFIG
# ==========================================================
#
# There is no more hardcoded list of locations/stations used at
# runtime - every dropdown, status grid, and validation check reads
# directly from the `stations` table (via get_active_locations() /
# get_stations_for_location() below), so adding, renaming, closing, or
# removing a location/station only ever requires editing that table
# (e.g. through the admin system's Manage Stations page) - never a
# code change here.
#
# The two constants below are used ONLY to seed that table the very
# first time the app runs against a brand-new, completely empty
# database (see ensure_schema()) - mirroring the same one-time seed in
# schema.sql - so the app has *something* to show before an admin has
# configured anything. They are never read anywhere else.
SEED_LOCATIONS = ["Main Learning Commons", "Learning Commons 2"]
SEED_STATIONS_PER_LOCATION = [f"Station {i}" for i in range(1, 11)]  # Station 1 ... Station 10

# ==========================================================
# APPOINTMENT STATUS
# ==========================================================

STATUS_RESERVED = "reserved"
STATUS_OCCUPIED = "occupied"
STATUS_EXPIRED = "expired"
STATUS_COMPLETED = "completed"

CHECKIN_GRACE_PERIOD_MINUTES = 15
EARLY_SCAN_WINDOW_MINUTES = 30

# ==========================================================
# BUSINESS HOURS
# ==========================================================
BUSINESS_START_TIME = time_cls(8, 0)   # 8:00 AM
BUSINESS_END_TIME = time_cls(17, 0)    # 5:00 PM

# ==========================================================
# EMAIL RESTRICTIONS
# ==========================================================
# Reservations are only accepted from this email domain. Mirrored
# client-side in dashboard.html for immediate feedback, but this
# server-side check is the authoritative one.
ALLOWED_EMAIL_DOMAIN = "gsfe.tupcavite.edu.ph"


# ==========================================================
# DATABASE HELPERS
# ==========================================================

def get_db_connection():
    """Opens a new connection to the MySQL (XAMPP) database."""
    return mysql.connector.connect(**DB_CONFIG)


def ensure_schema():
    """
    Makes sure every table/column this app depends on actually exists,
    the same way the admin system's own ensure_schema() does (see the
    comment in schema.sql). This is what schema.sql sets up when run
    manually in phpMyAdmin, but if the kiosk app is ever pointed at a
    fresh or partially-migrated database (e.g. only the original
    `appointments` table was created, without the later `restrictions`
    table, or that table's email column was never renamed to
    `gsfe_email`), queries like get_email_restriction()'s
    `SELECT ... FROM restrictions` fail outright, or silently never
    match anything - letting a restricted email straight through.
    Running this once at startup makes the app self-healing so that
    can't happen just because of DB setup drift.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # --- appointments table (base + later additions) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                id_number     VARCHAR(30)  NOT NULL UNIQUE,
                first_name    VARCHAR(100) NULL,
                last_name     VARCHAR(100) NULL,
                mi            VARCHAR(5),
                gsfe_email    VARCHAR(150) NOT NULL,
                date          DATE         NOT NULL,
                station_no    VARCHAR(50)  NOT NULL,
                location      VARCHAR(100) NOT NULL,
                start_time    TIME         NOT NULL,
                end_time      TIME         NOT NULL,
                created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Columns that were added after the original table (safe no-ops
        # if they already exist).
        appointment_columns = {
            "status": "VARCHAR(20) NOT NULL DEFAULT 'reserved'",
            "reservation_type": "VARCHAR(20) NOT NULL DEFAULT 'individual'",
            "group_id": "VARCHAR(30) NULL",
            "group_representative": "VARCHAR(150) NULL",
            "purpose": "VARCHAR(255) NULL",
        }
        cursor.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'appointments'
        """)
        existing_cols = {row[0] for row in cursor.fetchall()}
        for col_name, col_def in appointment_columns.items():
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE appointments ADD COLUMN {col_name} {col_def}")

        cursor.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'appointments'
              AND INDEX_NAME = 'idx_id_number'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("CREATE INDEX idx_id_number ON appointments (id_number)")

        cursor.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'appointments'
              AND INDEX_NAME = 'idx_group_id'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("CREATE INDEX idx_group_id ON appointments (group_id)")

        # --- restrictions table ---
        # This is the table get_email_restriction() reads from, keyed on
        # `gsfe_email` (matching the appointments table's own column
        # name). If this table/column is missing or misnamed, restricted
        # emails silently fail the check and slip through to booking.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS restrictions (
                id             INT AUTO_INCREMENT PRIMARY KEY,
                gsfe_email     VARCHAR(150) NOT NULL UNIQUE,
                reason         VARCHAR(255) NULL,
                restricted_by  VARCHAR(150) NULL,
                restricted_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'restrictions'
        """)
        restriction_cols = {row[0] for row in cursor.fetchall()}
        if "gsfe_email" not in restriction_cols:
            if "email" in restriction_cols:
                # Legacy column from an earlier version of this table -
                # rename it in place so existing restricted addresses
                # are preserved instead of lost.
                cursor.execute(
                    "ALTER TABLE restrictions CHANGE COLUMN email gsfe_email VARCHAR(150) NOT NULL UNIQUE"
                )
            else:
                cursor.execute("ALTER TABLE restrictions ADD COLUMN gsfe_email VARCHAR(150) NOT NULL UNIQUE")
        if "reason" not in restriction_cols:
            cursor.execute("ALTER TABLE restrictions ADD COLUMN reason VARCHAR(255) NULL")

        cursor.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'restrictions'
              AND INDEX_NAME = 'idx_restrictions_gsfe_email'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("CREATE INDEX idx_restrictions_gsfe_email ON restrictions (gsfe_email)")

        # --- stations table ---
        # This is what get_active_locations()/get_stations_for_location()
        # read from: `location` drives the Location dropdowns, and
        # `is_closed` drives each station's "closed" status. This table
        # is the single source of truth for locations/stations - there
        # is no constant fallback anymore.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                location    VARCHAR(100) NOT NULL,
                station_no  VARCHAR(50)  NOT NULL,
                is_closed   TINYINT(1)   NOT NULL DEFAULT 0,
                UNIQUE KEY uq_location_station (location, station_no)
            )
        """)
        cursor.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stations'
              AND INDEX_NAME = 'idx_stations_location'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("CREATE INDEX idx_stations_location ON stations (location)")

        # Seed the original 2 locations x 10 stations on a fresh install
        # only (never touches an existing configuration). This is the
        # ONLY place SEED_LOCATIONS/SEED_STATIONS_PER_LOCATION are used.
        cursor.execute("SELECT COUNT(*) FROM stations")
        if cursor.fetchone()[0] == 0:
            seed_rows = [
                (location, station_no)
                for location in SEED_LOCATIONS
                for station_no in SEED_STATIONS_PER_LOCATION
            ]
            cursor.executemany(
                "INSERT IGNORE INTO stations (location, station_no, is_closed) VALUES (%s, %s, 0)",
                seed_rows,
            )

        conn.commit()
        print("[ensure_schema] Database schema verified/updated successfully.")

    except mysql.connector.Error as db_err:
        # Don't crash the whole app on a schema-check hiccup - just log
        # it loudly so it's visible in the console/logs.
        print(f"[ensure_schema] WARNING: could not verify/update schema: {db_err}")

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def generate_id_number(cursor, appointment_date):
    """
    Builds an id_number in the format LC-MM-DD-YYYY-0001.
    The 4-digit counter resets back to 0001 for each distinct
    APPOINTMENT date (the date the reservation is FOR), not the
    date it happens to be booked/created on. `appointment_date` is
    the same "%Y-%m-%d" string that comes from data["date"].
    """
    appt_date = datetime.strptime(appointment_date, "%Y-%m-%d")
    date_str = appt_date.strftime("%m-%d-%Y")
    prefix = f"LC-{date_str}-"

    cursor.execute(
        "SELECT id_number FROM appointments WHERE id_number LIKE %s ORDER BY id DESC LIMIT 1 FOR UPDATE",
        (prefix + "%",),
    )
    row = cursor.fetchone()

    if row:
        last_seq = int(row["id_number"].split("-")[-1])
        new_seq = last_seq + 1
    else:
        new_seq = 1

    return f"{prefix}{new_seq:04d}"


def generate_group_id(cursor, appointment_date):
    """
    Builds a group_id in the format GRP-MM-DD-YYYY-0001, the same way
    generate_id_number() builds individual id_numbers - keyed off the
    APPOINTMENT date rather than the date the group is booked on.
    `appointment_date` is the same "%Y-%m-%d" string that comes from
    data["date"]. Shared by every station row created from one "Group
    Reservation" submission.
    """
    appt_date = datetime.strptime(appointment_date, "%Y-%m-%d")
    date_str = appt_date.strftime("%m-%d-%Y")
    prefix = f"GRP-{date_str}-"

    cursor.execute(
        "SELECT group_id FROM appointments WHERE group_id LIKE %s ORDER BY id DESC LIMIT 1 FOR UPDATE",
        (prefix + "%",),
    )
    row = cursor.fetchone()

    if row:
        last_seq = int(row["group_id"].split("-")[-1])
        new_seq = last_seq + 1
    else:
        new_seq = 1

    return f"{prefix}{new_seq:04d}"


def expire_stale_reservations(cursor, conn):
    """
    Flips any today's reservation to 'expired' (no-show) if it's still
    sitting at STATUS_RESERVED more than CHECKIN_GRACE_PERIOD_MINUTES
    after its start_time. Run this before reading station/timeline
    status so the grace-period cutoff is applied consistently.
    """
    today = datetime.now().date()
    grace = f"00:{CHECKIN_GRACE_PERIOD_MINUTES:02d}:00"

    cursor.execute(
        """
        UPDATE appointments
        SET status = %s
        WHERE status = %s
          AND date = %s
          AND ADDTIME(start_time, %s) < CURTIME()
        """,
        (STATUS_EXPIRED, STATUS_RESERVED, today, grace),
    )
    conn.commit()


def _to_time(value):
    """
    mysql-connector returns TIME columns as datetime.timedelta objects
    (not datetime.time). This normalizes either type into a time object
    so it can be compared against datetime.now().time().
    """
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return time_cls(hour=hours % 24, minute=minutes, second=seconds)
    return value  # already a time object


def _format_time(t):
    """Formats a time object as '8:00 AM' style text for the UI."""
    formatted = t.strftime("%I:%M %p")
    return formatted.lstrip("0") if formatted[0] == "0" else formatted


def _display_name(appt):
    """
    Builds a display name for an appointments row, whether it's an
    individual booking (first_name/mi/last_name) or a group booking
    (first_name/last_name are NULL; group_representative is used
    instead). Falls back gracefully if a row somehow has neither.
    """
    if appt.get("first_name") and appt.get("last_name"):
        mi = f"{appt['mi']}. " if appt.get("mi") else ""
        return f"{appt['first_name']} {mi}{appt['last_name']}".replace("  ", " ").strip()
    if appt.get("group_representative"):
        return f"{appt['group_representative']} (Group)"
    return "Unknown"


def _serialize_appointment_for_checkin(appt):
    """
    Converts an appointments row (as returned by the checkin query) into
    the plain-JSON shape the dashboard's displayAppointmentDetails()
    expects: formatted date/time strings instead of raw DB types.
    """
    start = _to_time(appt["start_time"])
    end = _to_time(appt["end_time"])

    return {
        "first_name": appt["first_name"],
        "last_name": appt["last_name"],
        "mi": appt["mi"],
        "display_name": _display_name(appt),
        "date": appt["date"].strftime("%B %d, %Y"),
        "start_time": _format_time(start),
        "end_time": _format_time(end),
        "location": appt["location"],
        "station_no": appt["station_no"],
    }


def get_active_locations(cursor):
    """
    Returns the list of locations currently active in the database -
    i.e. every distinct `stations.location` value that has at least one
    station which isn't closed - ordered alphabetically. This is what
    drives every "Location" dropdown on the dashboard, so a location
    added/closed/renamed via the admin system's station management is
    reflected here automatically.

    Returns an empty list if the stations table has no matching rows
    (e.g. every location has been closed, or none have been configured
    yet) - callers are responsible for handling that case rather than
    silently substituting a fake location.
    """
    cursor.execute(
        "SELECT DISTINCT location FROM stations WHERE is_closed = 0 ORDER BY location"
    )
    rows = cursor.fetchall()
    return [row["location"] for row in rows if row.get("location")]


def get_stations_for_location(cursor, location):
    """
    Returns every station configured for `location` in the `stations`
    table as an ordered list of {"station_no": ..., "is_closed": ...}
    dicts, sorted numerically (Station 1, Station 2, ... Station 10).
    This is what drives the station list (not just the status of an
    already-known list) shown/selectable for a given location, so
    stations added/removed/renamed via the admin system's Manage
    Stations page are reflected here automatically.

    Returns an empty list if the stations table has no rows for this
    location (e.g. it doesn't exist, or every station under it was
    deleted) - callers are responsible for handling that case rather
    than silently substituting a fake station list.
    """
    cursor.execute(
        "SELECT station_no, is_closed FROM stations WHERE location = %s",
        (location,),
    )
    rows = cursor.fetchall()
    if not rows:
        return []

    def _station_sort_key(row):
        match = re.search(r"(\d+)$", row["station_no"] or "")
        return int(match.group(1)) if match else float("inf")

    return sorted(
        (
            {"station_no": row["station_no"], "is_closed": bool(row["is_closed"])}
            for row in rows
        ),
        key=_station_sort_key,
    )


def get_default_location(cursor):
    """
    Returns the first active location from the database (alphabetically,
    per get_active_locations()), for routes that accept an optional
    `location` query param and need something sensible to fall back to
    when it's omitted. Returns None if no locations are active, so
    callers can report that clearly instead of guessing a location that
    may not exist.
    """
    locations = get_active_locations(cursor)
    return locations[0] if locations else None


def get_station_availability(cursor, date_str, location, start_time_str, end_time_str):
    """
    Returns { "Station 1": "vacant" | "reserved" | "closed", ... } for
    every station configured for `location` (via
    get_stations_for_location()), based on whether an existing
    (non-expired) booking on `date_str` / `location` overlaps the
    requested start_time_str/end_time_str window. Used to build the
    checkbox list on the Group Reservation tab so already-booked or
    closed stations can be disabled.

    Overlap rule: an existing appointment conflicts if
    existing.start_time < requested.end_time AND existing.end_time > requested.start_time
    """
    stations = get_stations_for_location(cursor, location)
    availability = {s["station_no"]: "vacant" for s in stations}

    # Stations closed via the admin system's Manage Stations page
    # (stations.is_closed) are reported as "closed" here so they're
    # excluded from booking the same way an already-booked station is,
    # but distinguishably from a real reservation.
    for s in stations:
        if s["is_closed"]:
            availability[s["station_no"]] = "closed"

    cursor.execute(
        """
        SELECT station_no
        FROM appointments
        WHERE date = %s
          AND location = %s
          AND status IN (%s, %s)
          AND start_time < %s
          AND end_time > %s
        """,
        (date_str, location, STATUS_RESERVED, STATUS_OCCUPIED, end_time_str, start_time_str),
    )

    for row in cursor.fetchall():
        raw_station = row["station_no"]
        if isinstance(raw_station, int):
            station = f"Station {raw_station}"
        elif isinstance(raw_station, str) and raw_station.isdigit():
            station = f"Station {raw_station}"
        else:
            station = raw_station

        if station in availability and availability[station] != "closed":
            availability[station] = "reserved"

    return availability


def is_valid_email_domain(email):
    """
    Returns True only if `email` is a simple address ending in
    "@gsfe.tupcavite.edu.ph" (case-insensitive). Used to enforce that only Gmail
    (GSFE) addresses can be used for reservations.
    """
    if not email or "@" not in email:
        return False

    local_part, _, domain = email.strip().rpartition("@")
    return bool(local_part) and domain.lower() == ALLOWED_EMAIL_DOMAIN


def get_email_restriction(cursor, gsfe_email):
    """
    Looks up `gsfe_email` (case-insensitive) in the `restrictions`
    table. Returns the matching row (dict, with an optional "reason")
    if the email is restricted, or None if it's clear to book.
    """
    try:
        cursor.execute(
            "SELECT id, gsfe_email, reason FROM restrictions WHERE LOWER(gsfe_email) = LOWER(%s) LIMIT 1",
            (gsfe_email,),
        )
        return cursor.fetchone()
    except mysql.connector.Error as db_err:
        # ensure_schema() should always keep this table/column in place,
        # but if the restrictions check itself is ever broken (schema
        # drift, permissions, etc.) don't let that take down the whole
        # booking flow - log it loudly and treat the email as
        # unrestricted rather than raising a raw SQL error to the user.
        # NOTE: this means a broken restrictions table fails OPEN
        # (bookings still go through) rather than blocking everyone -
        # keep an eye on the logs for this warning.
        print(f"[get_email_restriction] WARNING: restriction check failed, skipping: {db_err}")
        return None


def validate_business_hours(date_str, start_time_str, end_time_str):
    """
    Confirms a requested appointment falls on a weekday and entirely
    within BUSINESS_START_TIME - BUSINESS_END_TIME. Returns None if
    valid, or an error message string describing the first problem
    found.

    Expects date_str as 'YYYY-MM-DD' and *_time_str as 'HH:MM' (24-hour),
    which is what the dashboard's <input type="date"> / <input type="time">
    fields send.
    """
    try:
        appt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return "Invalid date format."

    # Monday=0 ... Sunday=6; weekday-only means Saturday(5)/Sunday(6) are out.
    if appt_date.weekday() >= 5:
        return "Reservations are only available Monday through Friday."

    try:
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        end_time = datetime.strptime(end_time_str, "%H:%M").time()
    except (ValueError, TypeError):
        return "Invalid time format."

    if start_time < BUSINESS_START_TIME or start_time > BUSINESS_END_TIME:
        return "Start Time must be between 8:00 AM and 5:00 PM."

    if end_time < BUSINESS_START_TIME or end_time > BUSINESS_END_TIME:
        return "End Time must be between 8:00 AM and 5:00 PM."

    if end_time <= start_time:
        return "End Time must be later than Start Time."

    return None


# ==========================================================
# QR CODE + EMAIL HELPERS
# ==========================================================

def generate_qr_code(id_number, data):
    """
    Generates a QR code (in memory) containing ONLY the appointment's
    id_number - nothing else about the student or reservation is
    encoded in the code itself. The scanner (see handleQrScan() in
    dashboard.html) reads this id_number back out and sends it to
    /api/checkin, which looks the appointment up fresh from the
    database and returns the current, authoritative details to
    populate the "Appointment Details" panel. Keeping personal details
    (name, email, etc.) out of the QR code itself means a printed or
    screenshotted code doesn't leak that information, and the details
    shown at check-in always reflect the live database row rather than
    a snapshot from whenever the code was generated.
    """
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(id_number)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def generate_group_station_qr_code(id_number, group_id, group_data, station_no):
    """
    Generates a QR code (in memory) for one station within a group
    booking. Same as generate_qr_code(): encodes ONLY that station's
    id_number, since /api/checkin looks everything else up from the
    database by id_number. group_id/station_no/etc. are unused params
    kept for call-site compatibility but are intentionally not encoded.
    """
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(id_number)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def send_confirmation_email(to_email, id_number, data, qr_buffer):
    """Emails the appointment confirmation + QR code to the student. Returns True/False."""
    try:
        full_name = f"{data['first_name']} {data['mi']}. {data['last_name']}"

        msg = MIMEMultipart()
        msg["From"] = EMAIL_CONFIG["sender_email"]
        msg["To"] = to_email
        msg["Subject"] = f"TUPC Learning Commons - Appointment Confirmation ({id_number})"

        body = f"""Hello {data['first_name']},

Your appointment at the TUPC Learning Commons has been successfully scheduled. Here are your reservation details:

Reference No: {id_number}
Name: {full_name}
Location: {data['location']}
Station No.: {data['station_no']}
Date: {data['date']}
Start Time: {data['start_time']}
End Time: {data['end_time']}

Please present the attached QR code to the kiosk/admin upon arrival so we can check you in.

REMINDERS:
- Please arrive on or before your scheduled start time.
- Your reservation will be automatically FORFEITED if you have not checked in
  (QR code scanned) within 15 MINUTES after your start time ({data['start_time']}).
- Once forfeited, the station will be released and made available to other students.
- Keep this email and the attached QR code accessible on your phone or printed,
  as it will be required for check-in.

Thank you,
TUPC Learning Commons
"""
        msg.attach(MIMEText(body, "plain"))

        qr_image = MIMEImage(qr_buffer.read(), name=f"{id_number}_qrcode.png")
        qr_image.add_header("Content-Disposition", "attachment", filename=f"{id_number}_qrcode.png")
        msg.attach(qr_image)

        server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"])
        server.starttls()
        server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
        server.sendmail(EMAIL_CONFIG["sender_email"], to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


def send_group_confirmation_email(to_email, group_id, group_data, station_bookings):
    """
    Emails one confirmation to the group representative covering every
    station in the group booking. `station_bookings` is a list of dicts,
    each: {"id_number": ..., "station_no": ..., "qr_buffer": BytesIO}.
    One QR code is attached per station (each carries its own id_number,
    so each station can still be checked in independently). Returns True/False.
    """
    try:
        stations_list = ", ".join(b["station_no"] for b in station_bookings)

        msg = MIMEMultipart()
        msg["From"] = EMAIL_CONFIG["sender_email"]
        msg["To"] = to_email
        msg["Subject"] = f"TUPC Learning Commons - Group Appointment Confirmation ({group_id})"

        ref_lines = "\n".join(
            f"  - {b['station_no']}: {b['id_number']}" for b in station_bookings
        )

        body = f"""Hello {group_data['group_representative']},

Your group appointment at the TUPC Learning Commons has been successfully scheduled. Here are your reservation details:

Group Reference No: {group_id}
Group Representative: {group_data['group_representative']}
Purpose: {group_data['purpose']}
Location: {group_data['location']}
Date: {group_data['date']}
Start Time: {group_data['start_time']}
End Time: {group_data['end_time']}

Reserved Stations ({len(station_bookings)}):
{ref_lines}

A separate QR code is attached for each reserved station. Please present the
matching QR code at each station upon arrival so we can check the group in.

REMINDERS:
- Please arrive on or before your scheduled start time.
- Each station's reservation will be automatically FORFEITED if it has not
  been checked in (QR code scanned) within 15 MINUTES after the start time
  ({group_data['start_time']}).
- Once forfeited, that station will be released and made available to other students.
- Keep this email and the attached QR codes accessible on your phone or printed,
  as they will be required for check-in.

Thank you,
TUPC Learning Commons
"""
        msg.attach(MIMEText(body, "plain"))

        for booking in station_bookings:
            qr_buffer = booking["qr_buffer"]
            qr_image = MIMEImage(
                qr_buffer.read(), name=f"{booking['id_number']}_qrcode.png"
            )
            qr_image.add_header(
                "Content-Disposition", "attachment",
                filename=f"{booking['id_number']}_qrcode.png",
            )
            msg.attach(qr_image)

        server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"])
        server.starttls()
        server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
        server.sendmail(EMAIL_CONFIG["sender_email"], to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


# ==========================================================
# ROUTES
# ==========================================================

# LOGIN
@app.route("/", methods=["GET", "POST"])
def login():
    return redirect(url_for("dashboard"))


# DASHBOARD
@app.route("/dashboard")
def dashboard():
    locations = []
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        locations = get_active_locations(cursor)
    except mysql.connector.Error as db_err:
        # The page still renders; dashboard.html's own /api/locations
        # poll (fetchLocations()) will pick up locations as soon as the
        # database is reachable again, so this doesn't need a fallback.
        print(f"[dashboard] WARNING: could not load locations: {db_err}")
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template("dashboard.html", locations=locations)


# ACTIVE LOCATIONS (polled by dashboard.html to keep every "Location"
# dropdown - the dashboard filter, individual reservation form, and
# group reservation form - in sync with the database instead of a
# hardcoded list).
@app.route("/api/locations")
def api_locations():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        locations = get_active_locations(cursor)
        return jsonify({"success": True, "locations": locations})

    except mysql.connector.Error as db_err:
        return jsonify({
            "success": False,
            "message": f"Database error: {db_err}",
            "locations": [],
        }), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# STATION LIST FOR A LOCATION (polled by dashboard.html to populate the
# "Station No." dropdown on the individual reservation form and the
# checkbox list on the Group Reservation form from the `stations` table
# - both the list itself and each entry's is_closed flag).
@app.route("/api/stations")
def api_stations():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        location = request.args.get("location") or get_default_location(cursor)
        if not location:
            return jsonify({
                "success": False,
                "message": "No active locations are configured.",
                "stations": [],
            }), 404
        stations = get_stations_for_location(cursor, location)
        return jsonify({"success": True, "location": location, "stations": stations})

    except mysql.connector.Error as db_err:
        return jsonify({
            "success": False,
            "message": f"Database error: {db_err}",
            "stations": [],
        }), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# REAL-TIME STATION STATUS (polled by dashboard.html)
@app.route("/api/station-status")
def station_status():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        location = request.args.get("location") or get_default_location(cursor)
        if not location:
            return jsonify({
                "success": False,
                "message": "No active locations are configured.",
            }), 404

        # Start every station configured for this location as vacant,
        # or "closed" if it's marked is_closed in the stations table -
        # station list and this baseline status both come straight from
        # the database.
        #
        # IMPORTANT: whether a station shows "closed" here depends ONLY
        # on stations.is_closed. It is never derived from the current
        # time of day / BUSINESS_START_TIME / BUSINESS_END_TIME - those
        # only gate whether a NEW appointment can be scheduled (see
        # validate_business_hours()), and must stay out of this
        # function. Toggling is_closed in the `stations` table (via the
        # admin system, or directly in the DB) is the only thing that
        # should ever change a station's displayed open/closed state.
        station_list = get_stations_for_location(cursor, location)
        stations = {
            s["station_no"]: {
                "status": "closed" if s["is_closed"] else "vacant",
                "start_time": None,
                "end_time": None,
            }
            for s in station_list
        }
        closed_stations = {s["station_no"] for s in station_list if s["is_closed"]}

        expire_stale_reservations(cursor, conn)

        today = datetime.now().date()
        cursor.execute(
            """
            SELECT station_no, start_time, end_time, status
            FROM appointments
            WHERE date = %s AND location = %s
            ORDER BY start_time ASC
            """,
            (today, location),
        )
        appointments = cursor.fetchall()

        now = datetime.now().time()

        for appt in appointments:
            # Normalize station identifier to match the keys built above
            # from the stations table. DB may store station_no as an
            # int (1..10) or as a string like "Station 1".
            raw_station = appt["station_no"]
            if isinstance(raw_station, int):
                station = f"Station {raw_station}"
            elif isinstance(raw_station, str) and raw_station.isdigit():
                station = f"Station {raw_station}"
            else:
                station = raw_station
            if station not in stations:
                # An appointment exists for a station no longer configured
                # for this location in the stations table - skip it.
                continue

            if station in closed_stations:
                # is_closed always wins - a booking made before the station
                # was closed shouldn't make it look occupied/reserved again.
                continue

            start = _to_time(appt["start_time"])
            end = _to_time(appt["end_time"])

            if appt.get("status") == STATUS_EXPIRED:
                continue  # no-show past the check-in grace period - station is vacant

            if now > end:
                continue  # appointment window already ended - ignore

            # Status is no longer inferred from the clock. A booking stays
            # "reserved" (even after start_time has passed) until the
            # (future) admin system marks it "occupied" - expected to
            # happen when the student's QR code is scanned at check-in.
            new_status = (
                STATUS_OCCUPIED
                if appt.get("status") == STATUS_OCCUPIED
                else STATUS_RESERVED
            )

            # "occupied" always wins; don't downgrade an occupied station
            if new_status == STATUS_OCCUPIED or stations[station]["status"] != STATUS_OCCUPIED:
                stations[station] = {
                    "status": new_status,
                    "start_time": _format_time(start),
                    "end_time": _format_time(end),
                }

        return jsonify({"success": True, "location": location, "stations": stations})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# DETAILED STATUS PAGE (more information about each station)
@app.route("/more-information", methods=["GET", "POST"])
@app.route("/detailed-status", methods=["GET", "POST"])
def detailed_status():
    locations = []
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        locations = get_active_locations(cursor)
    except mysql.connector.Error as db_err:
        # The page still renders; detailed_status.html's own
        # fetchLocations()/fetchStationSchedule() polling (against
        # /api/locations and /api/stations) picks up real data as soon
        # as the database is reachable, so no fallback is needed here.
        print(f"[detailed_status] WARNING: could not load locations: {db_err}")
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    # detailed_status.html builds its station list entirely client-side
    # from /api/locations and /api/stations - it never reads a
    # server-rendered `stations` variable - so nothing station-related
    # is passed here anymore.
    return render_template(
        "detailed_status.html",
        locations=locations,
    )

# DETAILED TIMELINE STATUS DATA (polled by detailed_status.html)
@app.route("/api/timeline-status")
def timeline_status():
    station_no = request.args.get("station_no")

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        location = request.args.get("location") or get_default_location(cursor)
        if not location:
            return jsonify({
                "success": False,
                "message": "No active locations are configured.",
            }), 404

        expire_stale_reservations(cursor, conn)

        today = datetime.now().date()
        query = """
            SELECT id_number, first_name, last_name, mi, gsfe_email,
                   date, station_no, location, start_time, end_time, status,
                   group_representative
            FROM appointments
            WHERE date = %s AND location = %s
            """
        params = [today, location]
        if station_no:
            normalized_station = station_no.strip()
            if normalized_station.lower().startswith("station "):
                station_key = normalized_station.split(" ", 1)[1]
            else:
                station_key = normalized_station

            if station_key.isdigit():
                query += " AND (station_no = %s OR station_no = %s)"
                params.extend([int(station_key), f"Station {station_key}"])
            else:
                query += " AND station_no = %s"
                params.append(normalized_station)
        query += " ORDER BY start_time ASC"

        cursor.execute(query, tuple(params))
        appointments = cursor.fetchall()

        now = datetime.now().time()
        timeline_entries = []

        for appt in appointments:
            # Normalize station identifier similar to station_status
            raw_station = appt["station_no"]
            if isinstance(raw_station, int):
                station_key = f"Station {raw_station}"
            elif isinstance(raw_station, str) and raw_station.isdigit():
                station_key = f"Station {raw_station}"
            else:
                station_key = raw_station

            start = _to_time(appt["start_time"])
            end = _to_time(appt["end_time"])

            if appt.get("status") == STATUS_EXPIRED:
                continue  # no-show past the check-in grace period - station is vacant

            if now > end:
                continue  # appointment window already ended - ignore

            # Same rule as /api/station-status: stay "reserved" past
            # start_time until an admin/QR check-in marks it "occupied".
            status = (
                STATUS_OCCUPIED
                if appt.get("status") == STATUS_OCCUPIED
                else STATUS_RESERVED
            )

            timeline_entries.append({
                "id_number": appt["id_number"],
                "station_no": station_key,
                "status": status,
                "start_time": _format_time(start),
                "end_time": _format_time(end),
                "name": _display_name(appt),
            })

        return jsonify({
            "success": True,
            "location": location,
            "station_no": station_no,
            "entries": timeline_entries,
        })

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# STATION SCHEDULE FOR A CHOSEN DAY (polled by detailed_status.html's
# fetchStationSchedule()). Unlike /api/timeline-status (which is locked
# to "today" and returns a flat list), this returns every appointment
# for the requested `date` (any day, past or future), grouped by
# station, so the detailed-status page can render the full timeline
# for whichever date the user picks in the date field.
@app.route("/api/station-schedule")
def station_schedule():
    date_str = request.args.get("date")

    if not date_str:
        return jsonify({"success": False, "message": "date is required."}), 400

    try:
        requested_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "message": "Invalid date format."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        location = request.args.get("location") or get_default_location(cursor)
        if not location:
            return jsonify({
                "success": False,
                "message": "No active locations are configured.",
            }), 404

        # Station list (and which of them are closed) comes straight
        # from the stations table.
        station_list = get_stations_for_location(cursor, location)
        stations = {s["station_no"]: [] for s in station_list}

        # Only expire stale reservations if we're looking at today - the
        # grace-period expiry rule only makes sense relative to the
        # current clock, not to a past or future date being reviewed.
        if requested_date == datetime.now().date():
            expire_stale_reservations(cursor, conn)

        cursor.execute(
            """
            SELECT station_no, start_time, end_time, status
            FROM appointments
            WHERE date = %s AND location = %s
            ORDER BY start_time ASC
            """,
            (requested_date, location),
        )

        for appt in cursor.fetchall():
            raw_station = appt["station_no"]
            if isinstance(raw_station, int):
                station = f"Station {raw_station}"
            elif isinstance(raw_station, str) and raw_station.isdigit():
                station = f"Station {raw_station}"
            else:
                station = raw_station
            if station not in stations:
                continue  # appointment for a station outside the configured list

            if appt.get("status") == STATUS_EXPIRED:
                continue  # no-show past the check-in grace period

            start = _to_time(appt["start_time"])
            end = _to_time(appt["end_time"])

            status = (
                STATUS_OCCUPIED
                if appt.get("status") == STATUS_OCCUPIED
                else STATUS_RESERVED
            )

            stations[station].append({
                "status": status,
                "start_time": _format_time(start),
                "end_time": _format_time(end),
            })

        return jsonify({"success": True, "location": location, "date": date_str, "stations": stations})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# SUBMIT APPOINTMENT (called via fetch() from dashboard.html)
@app.route("/submit-appointment", methods=["POST"])
def submit_appointment():
    data = request.get_json(silent=True) or {}

    required_fields = [
        "first_name", "last_name", "mi", "gsfe_email",
        "date", "station_no", "location", "start_time", "end_time",
    ]
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({
            "success": False,
            "message": f"Missing required field(s): {', '.join(missing)}",
        }), 400

    email = data["gsfe_email"].strip()
    if not is_valid_email_domain(email):
        return jsonify({
            "success": False,
            "message": f"Only @{ALLOWED_EMAIL_DOMAIN} email addresses are accepted for reservations.",
        }), 400

    schedule_error = validate_business_hours(
        data["date"], data["start_time"], data["end_time"]
    )
    if schedule_error:
        return jsonify({
            "success": False,
            "message": schedule_error,
        }), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        expire_stale_reservations(cursor, conn)

        # Block booking if this email is in the restrictions table
        # (e.g. flagged by an admin for prior no-shows / misuse).
        restriction = get_email_restriction(cursor, data["gsfe_email"])
        if restriction:
            reason = f" Reason: {restriction['reason']}" if restriction.get("reason") else ""
            return jsonify({
                "success": False,
                "message": (
                    "This email address is currently restricted from booking appointments due to multiple no-shows. Contact the Learning Commons Administratorfor assistance." + reason
                ),
            }), 403

        # Bug fix: this endpoint used to INSERT without checking whether
        # the station was already booked for the requested window, unlike
        # submit_group_appointment() which already does this check. Reuse
        # the same get_station_availability() helper so a station can't be
        # double-booked here either.
        availability = get_station_availability(
            cursor, data["date"], data["location"], data["start_time"], data["end_time"]
        )
        if availability.get(data["station_no"]) != "vacant":
            return jsonify({
                "success": False,
                "message": f"{data['station_no']} was just reserved by someone else for that time slot. Please pick a different station or time.",
            }), 409

        id_number = generate_id_number(cursor, data["date"])

        insert_query = """
            INSERT INTO appointments
                (id_number, first_name, last_name, mi, gsfe_email,
                 date, station_no, location, start_time, end_time, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(insert_query, (
            id_number,
            data["first_name"],
            data["last_name"],
            data["mi"],
            data["gsfe_email"],
            data["date"],
            data["station_no"],
            data["location"],
            data["start_time"],
            data["end_time"],
            STATUS_RESERVED,
        ))
        conn.commit()

        # Build + send the QR code confirmation email
        qr_buffer = generate_qr_code(id_number, data)
        email_sent = send_confirmation_email(data["gsfe_email"], id_number, data, qr_buffer)

        message = "Appointment successfully scheduled!"
        if not email_sent:
            message += " (Note: the confirmation email could not be sent — please check the email settings.)"

        return jsonify({
            "success": True,
            "message": message,
            "id_number": id_number,
            "email_sent": email_sent,
        })

    except mysql.connector.Error as db_err:
        return jsonify({
            "success": False,
            "message": f"Database error: {db_err}",
        }), 500

    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"An unexpected error occurred: {e}",
        }), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# GROUP RESERVATION — STATION AVAILABILITY (polled by dashboard.html
# whenever the date/location/start/end fields on the Group Reservation
# tab are all filled in, to disable already-booked stations in the
# checkbox list)
@app.route("/api/group-station-availability")
def group_station_availability():
    date_str = request.args.get("date")
    start_time_str = request.args.get("start_time")
    end_time_str = request.args.get("end_time")

    if not date_str or not start_time_str or not end_time_str:
        return jsonify({
            "success": False,
            "message": "date, start_time, and end_time are required.",
        }), 400

    schedule_error = validate_business_hours(date_str, start_time_str, end_time_str)
    if schedule_error:
        return jsonify({"success": False, "message": schedule_error}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        location = request.args.get("location") or get_default_location(cursor)
        if not location:
            return jsonify({
                "success": False,
                "message": "No active locations are configured.",
            }), 404

        expire_stale_reservations(cursor, conn)

        availability = get_station_availability(
            cursor, date_str, location, start_time_str, end_time_str
        )

        return jsonify({
            "success": True,
            "location": location,
            "date": date_str,
            "start_time": start_time_str,
            "end_time": end_time_str,
            "stations": availability,
        })

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# SUBMIT GROUP APPOINTMENT (called via fetch() from the Group Reservation
# tab in dashboard.html). Creates one appointments row PER selected
# station, all sharing the same group_id, so the existing station-status,
# timeline, and check-in logic keeps working unchanged.
@app.route("/submit-group-appointment", methods=["POST"])
def submit_group_appointment():
    data = request.get_json(silent=True) or {}

    required_fields = [
        "group_representative", "purpose", "gsfe_email",
        "date", "location", "start_time", "end_time", "stations",
    ]
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({
            "success": False,
            "message": f"Missing required field(s): {', '.join(missing)}",
        }), 400

    stations = data.get("stations")
    if not isinstance(stations, list) or len(stations) == 0:
        return jsonify({
            "success": False,
            "message": "Please select at least one station to reserve.",
        }), 400

    # De-duplicate now; validated against this location's actual station
    # list (from the stations table) once the DB connection is open below.
    stations = sorted(set(stations))

    email = data["gsfe_email"].strip()
    if not is_valid_email_domain(email):
        return jsonify({
            "success": False,
            "message": f"Only @{ALLOWED_EMAIL_DOMAIN} email addresses are accepted for reservations.",
        }), 400

    schedule_error = validate_business_hours(
        data["date"], data["start_time"], data["end_time"]
    )
    if schedule_error:
        return jsonify({"success": False, "message": schedule_error}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        expire_stale_reservations(cursor, conn)

        # Validate the submitted stations against this location's actual
        # station list (from the stations table), and put them in
        # numeric order for the confirmation email.
        location_stations = [
            s["station_no"] for s in get_stations_for_location(cursor, data["location"])
        ]
        stations = sorted(
            stations,
            key=lambda s: location_stations.index(s) if s in location_stations else 999,
        )
        invalid_stations = [s for s in stations if s not in location_stations]
        if invalid_stations:
            return jsonify({
                "success": False,
                "message": f"Unknown station(s) for {data['location']}: {', '.join(invalid_stations)}",
            }), 400

        # Block booking if this email is in the restrictions table
        # (e.g. flagged by an admin for prior no-shows / misuse).
        restriction = get_email_restriction(cursor, data["gsfe_email"])
        if restriction:
            reason = f" Reason: {restriction['reason']}" if restriction.get("reason") else ""
            return jsonify({
                "success": False,
                "message": (
                    "This email address is currently restricted from booking "
                    "the Learning Commons." + reason
                ),
            }), 403

        # Re-check availability server-side (authoritative check — the
        # client-side checkbox disabling is just a convenience) so two
        # people can't grab the same station in a race condition.
        availability = get_station_availability(
            cursor, data["date"], data["location"], data["start_time"], data["end_time"]
        )
        already_taken = [s for s in stations if availability.get(s) != "vacant"]
        if already_taken:
            return jsonify({
                "success": False,
                "message": (
                    "These station(s) were just reserved by someone else for that time slot: "
                    + ", ".join(already_taken)
                    + ". Please pick different stations or a different time."
                ),
            }), 409

        group_id = generate_group_id(cursor, data["date"])

        insert_query = """
            INSERT INTO appointments
                (id_number, reservation_type, group_id, group_representative, purpose,
                 gsfe_email, date, station_no, location, start_time, end_time, status)
            VALUES (%s, 'group', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        station_bookings = []
        for station_no in stations:
            id_number = generate_id_number(cursor, data["date"])
            cursor.execute(insert_query, (
                id_number,
                group_id,
                data["group_representative"],
                data["purpose"],
                data["gsfe_email"],
                data["date"],
                station_no,
                data["location"],
                data["start_time"],
                data["end_time"],
                STATUS_RESERVED,
            ))
            # Insert immediately so the NEXT generate_id_number() call in
            # this loop sees this row and increments past it correctly.
            conn.commit()

            qr_buffer = generate_group_station_qr_code(id_number, group_id, data, station_no)
            station_bookings.append({
                "id_number": id_number,
                "station_no": station_no,
                "qr_buffer": qr_buffer,
            })

        email_sent = send_group_confirmation_email(
            data["gsfe_email"], group_id, data, station_bookings
        )

        message = "Group appointment successfully scheduled!"
        if not email_sent:
            message += " (Note: the confirmation email could not be sent — please check the email settings.)"

        return jsonify({
            "success": True,
            "message": message,
            "group_id": group_id,
            "stations": [b["station_no"] for b in station_bookings],
            "id_numbers": [b["id_number"] for b in station_bookings],
            "email_sent": email_sent,
        })

    except mysql.connector.Error as db_err:
        return jsonify({
            "success": False,
            "message": f"Database error: {db_err}",
        }), 500

    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"An unexpected error occurred: {e}",
        }), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()



@app.route("/api/checkin", methods=["POST"])
def checkin():
    data = request.get_json(silent=True) or {}
    id_number = data.get("id_number")

    if not id_number:
        return jsonify({"success": False, "verification": "invalid", "message": "Missing id_number."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        expire_stale_reservations(cursor, conn)

        today = datetime.now().date()
        cursor.execute(
            """
            SELECT id_number, first_name, last_name, mi, date,
                   station_no, location, start_time, end_time, status,
                   group_representative
            FROM appointments
            WHERE id_number = %s
            """,
            (id_number,),
        )
        appt = cursor.fetchone()

        if appt is None:
            return jsonify({
                "success": False,
                "verification": "invalid",
                "message": "No appointment found for that QR code.",
            }), 404

        if appt["date"] != today:
            return jsonify({
                "success": False,
                "verification": "invalid",
                "message": "This appointment is not for today.",
                "appointment": _serialize_appointment_for_checkin(appt),
            }), 400

        # SIGN-OUT
        # Bug fix: scanning the SAME QR code again while the station was
        # "occupied" used to just report back "Already checked in" and
        # do nothing - the station stayed occupied forever unless an
        # admin manually intervened (or the kiosk had no way to ever
        # release it). Mirrors the admin system's sign-out behavior: the
        # row flips to "completed" and the station shows "vacant" again
        # on the next status refresh. The status only ever changes here,
        # in direct response to a scan - never automatically just
        # because time has passed.
        if appt["status"] == STATUS_OCCUPIED:
            cursor.execute(
                "UPDATE appointments SET status = %s WHERE id_number = %s",
                (STATUS_COMPLETED, id_number),
            )
            conn.commit()
            appt["status"] = STATUS_COMPLETED

            return jsonify({
                "success": True,
                "verification": "signed_out",
                "message": "Signed out successfully.",
                "id_number": id_number,
                "appointment": _serialize_appointment_for_checkin(appt),
            })

        if appt["status"] == STATUS_COMPLETED:
            return jsonify({
                "success": True,
                "verification": "signed_out",
                "message": "This appointment has already been signed out.",
                "id_number": id_number,
                "appointment": _serialize_appointment_for_checkin(appt),
            })

        if appt["status"] == STATUS_EXPIRED:
            return jsonify({
                "success": False,
                "verification": "expired",
                "message": f"This reservation expired after {CHECKIN_GRACE_PERIOD_MINUTES} minutes with no check-in.",
                "appointment": _serialize_appointment_for_checkin(appt),
            }), 400

        # TOO EARLY TO CHECK IN
        # Bug fix: this window didn't exist here at all, so the kiosk
        # would check a student in (and mark the station "occupied")
        # any amount of time before their actual start_time. Reported
        # as "invalid" in the UI, but deliberately does NOT touch the
        # database - the reservation stays "reserved" so it can still
        # be scanned normally later (closer to / after its start_time).
        start_dt = datetime.combine(today, _to_time(appt["start_time"]))
        minutes_until_start = (start_dt - datetime.now()).total_seconds() / 60.0

        if minutes_until_start >= EARLY_SCAN_WINDOW_MINUTES:
            return jsonify({
                "success": False,
                "verification": "invalid",
                "message": (
                    f"Too early to check in - this reservation starts at "
                    f"{_format_time(_to_time(appt['start_time']))}. Scanning opens "
                    f"{EARLY_SCAN_WINDOW_MINUTES} minutes before start_time."
                ),
                "appointment": _serialize_appointment_for_checkin(appt),
            }), 400

        cursor.execute(
            "UPDATE appointments SET status = %s WHERE id_number = %s",
            (STATUS_OCCUPIED, id_number),
        )
        conn.commit()
        appt["status"] = STATUS_OCCUPIED

        return jsonify({
            "success": True,
            "verification": "verified",
            "message": "Checked in successfully.",
            "id_number": id_number,
            "appointment": _serialize_appointment_for_checkin(appt),
        })

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "verification": "invalid", "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "verification": "invalid", "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# Verify/repair the database schema as soon as the module is imported,
# so this runs whether the app is started with `python app.py` or under
# a production WSGI server (gunicorn/waitress/etc.) that imports `app`
# directly and never hits the `__main__` block below.
ensure_schema()

if __name__ == "__main__":
    # host="0.0.0.0" makes this reachable from other devices on the same
    # WiFi/LAN (e.g. a tablet), not just this machine. Change the port
    # below if you're running kiosk and admin on the same machine at the
    # same time (they can't both use 5000).
    app.run(host="0.0.0.0", port=5000, debug=False)