import calendar as calendar_module
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

# ==========================================================
# STATION / LOCATION CONFIG
# ==========================================================
# Stations and locations used to be this fixed pair of Python lists.
# They're now stored in the database (see ensure_schema() below) so
# the Manage Stations page can add/remove stations and locations at
# runtime. These two lists are only used to SEED that data the first
# time the app runs against a fresh database - editing them after
# that has no effect; use the Manage Stations / Manage Locations UI
# instead.

STATION_LIST = [f"Station {i}" for i in range(1, 11)]  # Station 1 ... Station 10
LOCATION_LIST = ["Main Learning Commons", "Learning Commons 2"]

# ==========================================================
# APPOINTMENT STATUS
# ==========================================================
# A booking is no longer marked "occupied" just because the clock
# says start_time has passed. It stays "reserved" until someone
# (the not-yet-built admin system) actually checks the student in,
# which is expected to happen by scanning the QR code that was
# emailed to them. Only that action flips the row to "occupied".
#
# If check-in doesn't happen within CHECKIN_GRACE_PERIOD_MINUTES of
# start_time, the reservation is treated as a no-show, flipped to
# "expired", and the station shows "vacant" again.
#
# Once a booking is "occupied", scanning that SAME QR code again is
# treated as sign-out: the row flips to "completed" and the station
# goes back to showing "vacant". This only happens when the QR code
# is actually scanned and verified for sign-out - the station stays
# "occupied" indefinitely otherwise (no automatic timeout).
#
# This requires a `status` column on the `appointments` table:
#
#   ALTER TABLE appointments
#     ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'reserved';
#
# Valid values: 'reserved', 'occupied', 'expired', 'completed'. (A
# future admin UI could also add e.g. 'cancelled', but that isn't
# handled here yet.)
STATUS_RESERVED = "reserved"
STATUS_OCCUPIED = "occupied"
STATUS_EXPIRED = "expired"
STATUS_COMPLETED = "completed"

# If nobody checks a reservation in (QR scan) within this many minutes
# after its start_time, it's treated as a no-show: the row is flipped
# to STATUS_EXPIRED and the station goes back to showing "vacant".
CHECKIN_GRACE_PERIOD_MINUTES = 15

# If a QR code is scanned this many minutes (or more) *before* its
# reservation's start_time, the scan is reported back as "invalid" -
# it's simply too early to check in. This is a display-only rule: the
# reservation's status in the database is NOT changed, so the same QR
# code can be scanned again later (e.g. once it's within the window,
# or at its normal check-in time) and be verified normally.
EARLY_SCAN_WINDOW_MINUTES = 30

# A reservation shows up as an "upcoming" entry on the scheduling
# calendar's date cell once its start_time is within this many minutes
# of the current time (and hasn't ended yet). Requirement: "display
# the upcoming reservations within a 1 hour period".
UPCOMING_WINDOW_MINUTES = 60

# How long a Restrict action (Scheduling page -> False Appointments
# table) keeps a name/email from booking new appointments.
RESTRICTION_DURATION_DAYS = 30


# ==========================================================
# DATABASE HELPERS
# ==========================================================

def get_db_connection():
    """Opens a new connection to the MySQL (XAMPP) database."""
    return mysql.connector.connect(**DB_CONFIG)


# ==========================================================
# STATIONS / LOCATIONS SCHEMA (Manage Stations + Manage Locations)
# ==========================================================
# Stations and locations are real, editable rows now, not a fixed
# Python list. This needs two extra tables:
#
#   CREATE TABLE IF NOT EXISTS locations (
#       id   INT AUTO_INCREMENT PRIMARY KEY,
#       name VARCHAR(100) NOT NULL UNIQUE
#   );
#
#   CREATE TABLE IF NOT EXISTS stations (
#       id          INT AUTO_INCREMENT PRIMARY KEY,
#       station_no  VARCHAR(50)  NOT NULL,
#       location    VARCHAR(100) NOT NULL,
#       is_closed   TINYINT(1)   NOT NULL DEFAULT 0,
#       sort_order  INT          NOT NULL DEFAULT 0,
#       UNIQUE KEY uniq_station_location (station_no, location)
#   );
#
# ensure_schema() creates these automatically (and seeds them from
# STATION_LIST / LOCATION_LIST) the first time the app talks to a
# fresh database, so no manual migration step is required.

def ensure_schema(cursor, conn):
    """Creates the locations/stations tables if missing, and seeds
    them from STATION_LIST/LOCATION_LIST the first time they're empty."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS locations (
            id   INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL UNIQUE
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS stations (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            station_no  VARCHAR(50)  NOT NULL,
            location    VARCHAR(100) NOT NULL,
            is_closed   TINYINT(1)   NOT NULL DEFAULT 0,
            sort_order  INT          NOT NULL DEFAULT 0,
            UNIQUE KEY uniq_station_location (station_no, location)
        )
        """
    )
    conn.commit()

    cursor.execute("SELECT COUNT(*) AS c FROM locations")
    if cursor.fetchone()["c"] == 0:
        for name in LOCATION_LIST:
            cursor.execute("INSERT INTO locations (name) VALUES (%s)", (name,))
        conn.commit()

    cursor.execute("SELECT COUNT(*) AS c FROM stations")
    if cursor.fetchone()["c"] == 0:
        for location in LOCATION_LIST:
            for order, station_no in enumerate(STATION_LIST, start=1):
                cursor.execute(
                    """
                    INSERT INTO stations (station_no, location, is_closed, sort_order)
                    VALUES (%s, %s, 0, %s)
                    """,
                    (station_no, location, order),
                )
        conn.commit()

    # RESTRICTIONS (Scheduling page -> False Appointments table).
    # A row here means that name/email is (or was) barred from booking
    # new appointments. `active` + `restricted_until` together decide
    # whether the restriction currently applies - Unrestrict just flips
    # `active` back to 0 rather than deleting the history.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS restrictions (
            id                INT AUTO_INCREMENT PRIMARY KEY,
            first_name        VARCHAR(100),
            last_name         VARCHAR(100),
            mi                VARCHAR(10),
            gsfe_email        VARCHAR(150) NOT NULL,
            restricted_at     DATETIME NOT NULL,
            restricted_until  DATETIME NOT NULL,
            active            TINYINT(1) NOT NULL DEFAULT 1,
            UNIQUE KEY uniq_gsfe_email (gsfe_email)
        )
        """
    )
    conn.commit()


def normalize_station_label(raw_station):
    """DB may store an appointment's station_no as an int (1..10) or as
    a string like "Station 1" - this normalizes either into "Station N"
    so it can be matched against the `stations` table's station_no."""
    if isinstance(raw_station, int):
        return f"Station {raw_station}"
    if isinstance(raw_station, str) and raw_station.isdigit():
        return f"Station {raw_station}"
    return raw_station


def _station_sort_key(station_no):
    """Sorts "Station 1".."Station 10" (etc.) numerically instead of
    alphabetically, falling back to the end for anything unnumbered."""
    match = re.search(r"(\d+)$", station_no or "")
    return int(match.group(1)) if match else float("inf")


def get_locations(cursor):
    """Returns all location names, in the order they were added."""
    cursor.execute("SELECT name FROM locations ORDER BY id ASC")
    return [row["name"] for row in cursor.fetchall()]


def get_stations_for_location(cursor, location):
    """Returns [{"station_no":..., "is_closed":...}, ...] for one
    location, ordered the way they should be displayed/added-to."""
    cursor.execute(
        "SELECT station_no, is_closed FROM stations WHERE location = %s ORDER BY sort_order ASC, id ASC",
        (location,),
    )
    return cursor.fetchall()


def compute_station_statuses(cursor, conn, location):
    """
    Builds the live status for every configured station at `location`:
    "closed" (manually closed via Manage Stations), "vacant",
    "reserved", or "occupied" - derived from today's appointments plus
    each station's persisted is_closed flag. Returns a dict keyed by
    station_no: {"status", "start_time", "end_time", "is_closed"}.
    """
    expire_stale_reservations(cursor, conn)

    stations = {}
    for row in get_stations_for_location(cursor, location):
        is_closed = bool(row["is_closed"])
        stations[row["station_no"]] = {
            "status": "closed" if is_closed else "vacant",
            "start_time": None,
            "end_time": None,
            "is_closed": is_closed,
        }

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
        station = normalize_station_label(appt["station_no"])
        if station not in stations or stations[station]["is_closed"]:
            continue  # unknown station, or manually closed - always shows "closed"

        if appt.get("status") in (STATUS_EXPIRED, STATUS_COMPLETED):
            continue  # no-show / signed out early - station is vacant

        start = _to_time(appt["start_time"])
        end = _to_time(appt["end_time"])

        if now > end:
            continue  # appointment window already ended - ignore

        new_status = (
            STATUS_OCCUPIED
            if appt.get("status") == STATUS_OCCUPIED
            else STATUS_RESERVED
        )

        # "occupied" always wins; don't downgrade an occupied station
        if new_status == STATUS_OCCUPIED or stations[station]["status"] != STATUS_OCCUPIED:
            stations[station]["status"] = new_status
            stations[station]["start_time"] = _format_time(start)
            stations[station]["end_time"] = _format_time(end)

    return stations


def generate_id_number(cursor):
    """
    Builds an id_number in the format LC-MM-DD-YYYY-0001.
    The 4-digit counter resets back to 0001 every new day,

    based on the current server date.
    """
    today = datetime.now()
    date_str = today.strftime("%m-%d-%Y")
    prefix = f"LC-{date_str}-"

    cursor.execute(
        "SELECT id_number FROM appointments WHERE id_number LIKE %s ORDER BY id DESC LIMIT 1",
        (prefix + "%",),
    )
    row = cursor.fetchone()

    if row:
        last_seq = int(row["id_number"].split("-")[-1])
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


def get_active_restriction(cursor, first_name, last_name, gsfe_email):
    """
    Returns the active restrictions row blocking this student from
    booking, or None. Matches on gsfe_email OR (first_name + last_name)
    - either one being restricted is enough to block the booking, per
    the "restrict the name and/or email" requirement.
    """
    cursor.execute(
        """
        SELECT * FROM restrictions
        WHERE active = 1
          AND restricted_until > NOW()
          AND (
                gsfe_email = %s
                OR (first_name = %s AND last_name = %s)
              )
        ORDER BY restricted_until DESC
        LIMIT 1
        """,
        (gsfe_email, first_name, last_name),
    )
    return cursor.fetchone()


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


# ==========================================================
# QR CODE + EMAIL HELPERS
# ==========================================================

def generate_qr_code(id_number, data):
    """Generates a QR code (in memory) containing the appointment details."""
    qr_payload = {
        "id_number": id_number,
        "name": f"{data['first_name']} {data['mi']}. {data['last_name']}",
        "email": data["gsfe_email"],
        "date": data["date"],
        "station_no": data["station_no"],
        "location": data["location"],
        "start_time": data["start_time"],
        "end_time": data["end_time"],
    }

    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(json.dumps(qr_payload))
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def send_confirmation_email(to_email, id_number, data, qr_buffer):
    """Emails the appointment confirmation + QR code to the student. Returns True/False."""
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_CONFIG["sender_email"]
        msg["To"] = to_email
        msg["Subject"] = f"TUPC Learning Commons - Appointment Confirmation ({id_number})"

        body = f"""Hello {data['first_name']},

Your appointment at the TUPC Learning Commons has been successfully scheduled.

Reference No: {id_number}
Date: {data['date']}
Station: {data['station_no']}
Location: {data['location']}
Time: {data['start_time']} - {data['end_time']}

Please present the attached QR code upon arrival.

Thank you,
TUPC Learning Commons
"""
        msg.attach(MIMEText(body, "plain"))

        qr_image = MIMEImage(qr_buffer.read(), name=f"{id_number}_qrcode.png")
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


def send_restriction_email(to_email, first_name, restricted_until):
    """Emails a student notifying them of a temporary restriction. Returns True/False."""
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_CONFIG["sender_email"]
        msg["To"] = to_email
        msg["Subject"] = "TUPC Learning Commons - Temporary Access Restriction"

        until_str = restricted_until.strftime("%B %d, %Y")
        body = f"""Hello {first_name or 'Student'},

This is to inform you that your access to the TUPC Learning Commons has been temporarily restricted for 1 month, effective immediately, due to repeated unverified ("false") appointments - reservations that were not checked in within {CHECKIN_GRACE_PERIOD_MINUTES} minutes of their start time.

You will not be able to schedule new appointments at the Learning Commons until {until_str}.

If you believe this is a mistake, please contact the Learning Commons administrator.

Thank you,
TUPC Learning Commons
"""
        msg.attach(MIMEText(body, "plain"))

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
    return render_template("dashboard.html")

@app.route("/manage-stations")
def manage_stations():
    return render_template("manage-stations.html", active_page="manage-stations")

@app.route("/scheduling")
def scheduling():
    return render_template("scheduling.html", active_page="scheduling")

@app.route("/login-management")
def login_management():
    return render_template("login-management.html", active_page="login-management")

@app.route("/ongoing-sessions")
def ongoing_sessions():
    return render_template("ongoing-sessions.html", active_page="ongoing-sessions")


def _serialize_appointment_row(row):
    """Shared shape for the Ongoing Sessions + Appointment History tables.
    Includes both display-formatted fields (date, start_time, end_time)
    and raw sort-friendly fields (date_sort, start_time_sort,
    end_time_sort, station_sort, name_sort) so the frontend can sort
    without having to re-parse "8:00 AM" / "July 17, 2026" strings."""
    date_val = row["date"]
    start_t = _to_time(row["start_time"])
    end_t = _to_time(row["end_time"])
    station_label = normalize_station_label(row["station_no"])
    middle_initial = f"{row['mi']}. " if row.get("mi") else ""

    return {
        "id_number": row["id_number"],
        "first_name": row["first_name"],
        "mi": row["mi"],
        "last_name": row["last_name"],
        "gsfe_email": row["gsfe_email"],
        "full_name": f"{row['first_name']} {middle_initial}{row['last_name']}",
        "name_sort": f"{row['last_name']} {row['first_name']}".lower(),
        "date": date_val.strftime("%B %d, %Y") if hasattr(date_val, "strftime") else date_val,
        "date_sort": date_val.isoformat() if hasattr(date_val, "isoformat") else str(date_val),
        "start_time": _format_time(start_t),
        "start_time_sort": start_t.strftime("%H:%M:%S"),
        "end_time": _format_time(end_t),
        "end_time_sort": end_t.strftime("%H:%M:%S"),
        "location": row["location"],
        "station_no": station_label,
        "station_sort": _station_sort_key(station_label),
        "status": row["status"],
    }


# ONGOING SESSIONS DATA (polled by ongoing-sessions.html)
# Every appointment currently "occupied" (i.e. the student has checked
# in via QR scan and hasn't been signed out yet), across all stations
# and locations.
@app.route("/api/ongoing-sessions")
def ongoing_sessions_data():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)
        expire_stale_reservations(cursor, conn)

        cursor.execute(
            """
            SELECT id_number, first_name, last_name, mi, gsfe_email,
                   date, station_no, location, start_time, end_time, status
            FROM appointments
            WHERE status = %s
            ORDER BY date ASC, start_time ASC
            """,
            (STATUS_OCCUPIED,),
        )
        rows = cursor.fetchall()

        sessions = [_serialize_appointment_row(row) for row in rows]

        return jsonify({"success": True, "sessions": sessions})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# APPOINTMENT HISTORY DATA (polled by login-management.html)
# Every appointment ever made, regardless of status (reserved,
# occupied, expired, completed). Sorting is handled client-side (the
# "Sort by" + ascending/descending controls) so this always returns
# the full set, sorted by date as a sensible default order.
@app.route("/api/appointment-history")
def appointment_history_data():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)
        expire_stale_reservations(cursor, conn)

        cursor.execute(
            """
            SELECT id_number, first_name, last_name, mi, gsfe_email,
                   date, station_no, location, start_time, end_time, status
            FROM appointments
            ORDER BY date DESC, start_time DESC
            """
        )
        rows = cursor.fetchall()

        history = [_serialize_appointment_row(row) for row in rows]

        return jsonify({"success": True, "history": history})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# REAL-TIME STATION STATUS (polled by dashboard.html)
@app.route("/api/station-status")
def station_status():
    location = request.args.get("location", LOCATION_LIST[0])

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        computed = compute_station_statuses(cursor, conn, location)

        # This endpoint's public shape stays the same as before
        # (no is_closed key) - it's only consumed for display.
        stations = {
            name: {
                "status": info["status"],
                "start_time": info["start_time"],
                "end_time": info["end_time"],
            }
            for name, info in computed.items()
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


# ==========================================================
# SCHEDULING PAGE - CALENDAR + FALSE APPOINTMENTS
# ==========================================================

# LIVE CALENDAR DATA (polled by scheduling.html)
# Returns, for every day of the given month/location, whether it has
# any reserved/occupied appointments plus the subset of those
# appointments that are "upcoming" - starting within
# UPCOMING_WINDOW_MINUTES of right now. The frontend uses this to
# build the calendar grid around the real date and to print
# "(station_no) start_time - end_time" on the relevant day cell.
@app.route("/api/calendar-status")
def calendar_status():
    location = request.args.get("location", LOCATION_LIST[0])

    now = datetime.now()
    try:
        year = int(request.args.get("year", now.year))
        month = int(request.args.get("month", now.month))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "year and month must be integers."}), 400

    if month < 1 or month > 12:
        return jsonify({"success": False, "message": "month must be between 1 and 12."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)
        expire_stale_reservations(cursor, conn)

        first_day = datetime(year, month, 1).date()
        last_day_num = calendar_module.monthrange(year, month)[1]
        last_day = datetime(year, month, last_day_num).date()

        cursor.execute(
            """
            SELECT date, station_no, start_time, end_time, status
            FROM appointments
            WHERE location = %s AND date BETWEEN %s AND %s
            ORDER BY date ASC, start_time ASC
            """,
            (location, first_day, last_day),
        )
        rows = cursor.fetchall()

        days = {}
        for row in rows:
            if row["status"] in (STATUS_EXPIRED, STATUS_COMPLETED):
                continue  # no-show / signed out - doesn't mark the day

            day_key = row["date"].isoformat()
            info = days.setdefault(day_key, {
                "has_reserved": False,
                "has_occupied": False,
                "upcoming": [],
            })

            if row["status"] == STATUS_OCCUPIED:
                info["has_occupied"] = True
            else:
                info["has_reserved"] = True

            start_t = _to_time(row["start_time"])
            end_t = _to_time(row["end_time"])
            start_dt = datetime.combine(row["date"], start_t)
            end_dt = datetime.combine(row["date"], end_t)

            minutes_until_start = (start_dt - now).total_seconds() / 60.0
            is_upcoming = (
                0 <= minutes_until_start <= UPCOMING_WINDOW_MINUTES
                or (start_dt <= now < end_dt)  # already started, still ongoing
            )
            if is_upcoming:
                info["upcoming"].append({
                    "station_no": normalize_station_label(row["station_no"]),
                    "start_time": _format_time(start_t),
                    "end_time": _format_time(end_t),
                    "status": row["status"],
                })

        return jsonify({
            "success": True,
            "location": location,
            "year": year,
            "month": month,
            "today": now.date().isoformat(),
            "days": days,
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


# FALSE APPOINTMENTS (polled by scheduling.html)
# A "false appointment" = a reservation nobody checked in within
# CHECKIN_GRACE_PERIOD_MINUTES of its start_time, i.e. status was
# flipped to STATUS_EXPIRED by expire_stale_reservations(). Grouped by
# student (gsfe_email) with a count of how many they've racked up, plus
# whether they're currently restricted.
@app.route("/api/false-appointments")
def false_appointments():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)
        expire_stale_reservations(cursor, conn)

        cursor.execute(
            """
            SELECT first_name, last_name, mi, gsfe_email,
                   COUNT(*) AS strikes,
                   MAX(date) AS last_date
            FROM appointments
            WHERE status = %s
            GROUP BY gsfe_email, first_name, last_name, mi
            ORDER BY strikes DESC, last_date DESC
            """,
            (STATUS_EXPIRED,),
        )
        rows = cursor.fetchall()

        cursor.execute("SELECT gsfe_email, restricted_until, active FROM restrictions")
        restriction_map = {r["gsfe_email"]: r for r in cursor.fetchall()}

        now = datetime.now()
        students = []
        for row in rows:
            restriction = restriction_map.get(row["gsfe_email"])
            is_restricted = bool(
                restriction and restriction["active"] and restriction["restricted_until"] > now
            )
            students.append({
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "mi": row["mi"],
                "gsfe_email": row["gsfe_email"],
                "strikes": row["strikes"],
                "restricted": is_restricted,
                "restricted_until": (
                    restriction["restricted_until"].strftime("%B %d, %Y") if is_restricted else None
                ),
            })

        return jsonify({"success": True, "students": students})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# RESTRICT - blocks a name/email from booking new appointments for
# RESTRICTION_DURATION_DAYS and emails them a notice.
@app.route("/api/restrict", methods=["POST"])
def restrict_student():
    data = request.get_json(silent=True) or {}
    gsfe_email = (data.get("gsfe_email") or "").strip()
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    mi = (data.get("mi") or "").strip()

    if not gsfe_email:
        return jsonify({"success": False, "message": "gsfe_email is required."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        now = datetime.now()
        restricted_until = now + timedelta(days=RESTRICTION_DURATION_DAYS)

        cursor.execute(
            """
            INSERT INTO restrictions
                (first_name, last_name, mi, gsfe_email, restricted_at, restricted_until, active)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
            ON DUPLICATE KEY UPDATE
                first_name = VALUES(first_name),
                last_name = VALUES(last_name),
                mi = VALUES(mi),
                restricted_at = VALUES(restricted_at),
                restricted_until = VALUES(restricted_until),
                active = 1
            """,
            (first_name, last_name, mi, gsfe_email, now, restricted_until),
        )
        conn.commit()

        email_sent = send_restriction_email(gsfe_email, first_name, restricted_until)

        return jsonify({
            "success": True,
            "gsfe_email": gsfe_email,
            "restricted_until": restricted_until.strftime("%B %d, %Y"),
            "email_sent": email_sent,
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


# UNRESTRICT - lifts a restriction early.
@app.route("/api/unrestrict", methods=["POST"])
def unrestrict_student():
    data = request.get_json(silent=True) or {}
    gsfe_email = (data.get("gsfe_email") or "").strip()

    if not gsfe_email:
        return jsonify({"success": False, "message": "gsfe_email is required."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        cursor.execute(
            "UPDATE restrictions SET active = 0 WHERE gsfe_email = %s",
            (gsfe_email,),
        )
        conn.commit()

        return jsonify({"success": True, "gsfe_email": gsfe_email})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# ==========================================================
# STATION / LOCATION MANAGEMENT (Manage Stations page)
# ==========================================================

# STATION LIST WITH LIVE STATUS (polled by manage-stations.html)
@app.route("/api/manage-stations")
def manage_stations_status():
    location = request.args.get("location", LOCATION_LIST[0])

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        computed = compute_station_statuses(cursor, conn, location)

        stations = [
            {
                "station_no": name,
                "status": info["status"],
                "is_closed": info["is_closed"],
            }
            for name, info in computed.items()
        ]
        stations.sort(key=lambda s: _station_sort_key(s["station_no"]))

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


# CLOSE / REOPEN A STATION - closing makes it show "closed" on the
# Availability Status panel and blocks new reservations against it.
@app.route("/api/stations/close", methods=["POST"])
def set_station_closed():
    data = request.get_json(silent=True) or {}
    station_no = data.get("station_no")
    location = data.get("location")
    closed = bool(data.get("closed"))

    if not station_no or not location:
        return jsonify({"success": False, "message": "station_no and location are required."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        cursor.execute(
            "UPDATE stations SET is_closed = %s WHERE station_no = %s AND location = %s",
            (1 if closed else 0, station_no, location),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify({"success": False, "message": "Station not found."}), 404

        return jsonify({
            "success": True,
            "station_no": station_no,
            "location": location,
            "is_closed": closed,
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


# ADD A LEARNING STATION - appended to the bottom of the given
# location's station list (next numbered "Station N").
@app.route("/api/stations", methods=["POST"])
def add_station():
    data = request.get_json(silent=True) or {}
    location = data.get("location")

    if not location:
        return jsonify({"success": False, "message": "location is required."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        cursor.execute(
            "SELECT station_no, sort_order FROM stations WHERE location = %s ORDER BY sort_order DESC, id DESC LIMIT 1",
            (location,),
        )
        last = cursor.fetchone()

        next_sort = (last["sort_order"] + 1) if last else 1
        next_num = (_station_sort_key(last["station_no"]) + 1) if last and last["station_no"] else next_sort
        if next_num == float("inf"):
            next_num = next_sort
        station_no = f"Station {int(next_num)}"

        cursor.execute(
            """
            INSERT INTO stations (station_no, location, is_closed, sort_order)
            VALUES (%s, %s, 0, %s)
            """,
            (station_no, location, next_sort),
        )
        conn.commit()

        return jsonify({"success": True, "station_no": station_no, "location": location})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# REMOVE A LEARNING STATION
@app.route("/api/stations", methods=["DELETE"])
def remove_station():
    data = request.get_json(silent=True) or {}
    station_no = data.get("station_no")
    location = data.get("location")

    if not station_no or not location:
        return jsonify({"success": False, "message": "station_no and location are required."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        cursor.execute(
            "DELETE FROM stations WHERE station_no = %s AND location = %s",
            (station_no, location),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify({"success": False, "message": "Station not found."}), 404

        return jsonify({"success": True})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# LIST LOCATIONS (polled by the Manage Locations overlay)
@app.route("/api/locations", methods=["GET"])
def list_locations():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        return jsonify({"success": True, "locations": get_locations(cursor)})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# ADD A LOCATION
@app.route("/api/locations", methods=["POST"])
def add_location():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"success": False, "message": "Location name is required."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        cursor.execute("SELECT id FROM locations WHERE name = %s", (name,))
        if cursor.fetchone():
            return jsonify({"success": False, "message": "That location already exists."}), 400

        cursor.execute("INSERT INTO locations (name) VALUES (%s)", (name,))
        conn.commit()

        return jsonify({"success": True, "name": name})

    except mysql.connector.Error as db_err:
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# REMOVE A LOCATION (also removes its stations)
@app.route("/api/locations", methods=["DELETE"])
def remove_location():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"success": False, "message": "Location name is required."}), 400

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        cursor.execute("DELETE FROM stations WHERE location = %s", (name,))
        cursor.execute("DELETE FROM locations WHERE name = %s", (name,))
        deleted = cursor.rowcount
        conn.commit()

        if deleted == 0:
            return jsonify({"success": False, "message": "Location not found."}), 404

        return jsonify({"success": True})

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
    return render_template(
        "detailed_status.html",
        stations=STATION_LIST,
        locations=LOCATION_LIST,
    )

# DETAILED TIMELINE STATUS DATA (polled by detailed_status.html)
@app.route("/api/timeline-status")
def timeline_status():
    location = request.args.get("location", LOCATION_LIST[0])
    station_no = request.args.get("station_no")

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        expire_stale_reservations(cursor, conn)

        today = datetime.now().date()
        query = """
            SELECT id_number, first_name, last_name, mi, gsfe_email,
                   date, station_no, location, start_time, end_time, status
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

            if appt.get("status") == STATUS_COMPLETED:
                continue  # student scanned out early - station is vacant again

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
                "name": f"{appt['first_name']} {appt['mi']}. {appt['last_name']}",
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

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_schema(cursor, conn)

        # A name/email restricted via the False Appointments table
        # (Scheduling page) cannot book a new appointment.
        restriction = get_active_restriction(
            cursor, data["first_name"], data["last_name"], data["gsfe_email"]
        )
        if restriction:
            until_str = restriction["restricted_until"].strftime("%B %d, %Y")
            return jsonify({
                "success": False,
                "message": (
                    f"This name/email is temporarily restricted from booking at the "
                    f"Learning Commons until {until_str}."
                ),
            }), 403

        # A station closed via Manage Stations is not bookable.
        station_label = normalize_station_label(data["station_no"])
        cursor.execute(
            "SELECT is_closed FROM stations WHERE station_no = %s AND location = %s",
            (station_label, data["location"]),
        )
        station_row = cursor.fetchone()
        if station_row and station_row["is_closed"]:
            return jsonify({
                "success": False,
                "message": f"{station_label} is currently closed and not accepting reservations.",
            }), 400

        id_number = generate_id_number(cursor)

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


# CHECK-IN (called once the admin system / QR scanner reads a student's
# QR code at the station - this is what actually flips a booking from
# "reserved" to "occupied". Also used directly by the dashboard's QR
# Code Verification panel: it reports back a "verification" status
# ("verified" / "not_verified" / "expired") plus the appointment's
# details so they can be displayed next to the scan.)
@app.route("/api/checkin", methods=["POST"])
def checkin():
    data = request.get_json(silent=True) or {}
    id_number = data.get("id_number")

    if not id_number:
        return jsonify({
            "success": False,
            "verification": "invalid",
            "message": "Missing id_number.",
            "appointment": None,
        }), 400

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
                   station_no, location, start_time, end_time, status
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
                "appointment": None,
            }), 404

        # Normalize station identifier for display, same as the other routes.
        raw_station = appt["station_no"]
        if isinstance(raw_station, int):
            station_display = f"Station {raw_station}"
        elif isinstance(raw_station, str) and raw_station.isdigit():
            station_display = f"Station {raw_station}"
        else:
            station_display = raw_station

        appointment_info = {
            "id_number": appt["id_number"],
            "first_name": appt["first_name"],
            "mi": appt["mi"],
            "last_name": appt["last_name"],
            "date": appt["date"].strftime("%B %d, %Y") if hasattr(appt["date"], "strftime") else appt["date"],
            "start_time": _format_time(_to_time(appt["start_time"])),
            "end_time": _format_time(_to_time(appt["end_time"])),
            "location": appt["location"],
            "station_no": station_display,
        }

        if appt["date"] != today:
            return jsonify({
                "success": False,
                "verification": "invalid",
                "message": "This appointment is not for today.",
                "appointment": appointment_info,
            }), 400

        # SIGN-OUT
        # Scanning the SAME QR code again while the station is "occupied"
        # signs the student out: the row flips to "completed" and the
        # station shows "vacant" again on the next status refresh. The
        # status only ever changes here, in direct response to a scan -
        # never automatically just because time has passed.
        if appt["status"] == STATUS_OCCUPIED:
            cursor.execute(
                "UPDATE appointments SET status = %s WHERE id_number = %s",
                (STATUS_COMPLETED, id_number),
            )
            conn.commit()

            return jsonify({
                "success": True,
                "verification": "signed_out",
                "message": "Signed out successfully.",
                "appointment": appointment_info,
            })

        if appt["status"] == STATUS_COMPLETED:
            return jsonify({
                "success": True,
                "verification": "signed_out",
                "message": "This appointment has already been signed out.",
                "appointment": appointment_info,
            })

        if appt["status"] == STATUS_EXPIRED:
            return jsonify({
                "success": False,
                "verification": "expired",
                "message": f"This reservation expired after {CHECKIN_GRACE_PERIOD_MINUTES} minutes with no check-in.",
                "appointment": appointment_info,
            }), 400

        # TOO EARLY TO CHECK IN
        # Reported as "invalid" in the UI, but deliberately does NOT touch
        # the database - the reservation stays "reserved" so it can still
        # be scanned normally later (closer to / after its start_time).
        start_dt = datetime.combine(today, _to_time(appt["start_time"]))
        minutes_until_start = (start_dt - datetime.now()).total_seconds() / 60.0

        if minutes_until_start >= EARLY_SCAN_WINDOW_MINUTES:
            return jsonify({
                "success": False,
                "verification": "invalid",
                "message": (
                    f"Too early to check in - this reservation starts at "
                    f"{appointment_info['start_time']}. Scanning opens "
                    f"{EARLY_SCAN_WINDOW_MINUTES} minutes before start_time."
                ),
                "appointment": appointment_info,
            }), 400

        cursor.execute(
            "UPDATE appointments SET status = %s WHERE id_number = %s",
            (STATUS_OCCUPIED, id_number),
        )
        conn.commit()

        return jsonify({
            "success": True,
            "verification": "verified",
            "message": "Checked in successfully.",
            "appointment": appointment_info,
        })

    except mysql.connector.Error as db_err:
        return jsonify({
            "success": False,
            "verification": "invalid",
            "message": f"Database error: {db_err}",
            "appointment": None,
        }), 500

    except Exception as e:
        return jsonify({
            "success": False,
            "verification": "invalid",
            "message": f"An unexpected error occurred: {e}",
            "appointment": None,
        }), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    app.run(debug=True)