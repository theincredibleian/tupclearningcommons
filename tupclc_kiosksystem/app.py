import io
import json
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

STATION_LIST = [f"Station {i}" for i in range(1, 11)]  # Station 1 ... Station 10
LOCATION_LIST = ["Main Learning Commons", "Learning Commons 2"]

# ==========================================================
# APPOINTMENT STATUS
# ==========================================================

STATUS_RESERVED = "reserved"
STATUS_OCCUPIED = "occupied"
STATUS_EXPIRED = "expired"

CHECKIN_GRACE_PERIOD_MINUTES = 15

# ==========================================================
# BUSINESS HOURS
# ==========================================================
BUSINESS_START_TIME = time_cls(8, 0)   # 8:00 AM
BUSINESS_END_TIME = time_cls(17, 0)    # 5:00 PM


# ==========================================================
# DATABASE HELPERS
# ==========================================================

def get_db_connection():
    """Opens a new connection to the MySQL (XAMPP) database."""
    return mysql.connector.connect(**DB_CONFIG)


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


def generate_group_id(cursor):
    """
    Builds a group_id in the format GRP-MM-DD-YYYY-0001, the same way
    generate_id_number() builds individual id_numbers. Shared by every
    station row created from one "Group Reservation" submission.
    """
    today = datetime.now()
    date_str = today.strftime("%m-%d-%Y")
    prefix = f"GRP-{date_str}-"

    cursor.execute(
        "SELECT group_id FROM appointments WHERE group_id LIKE %s ORDER BY id DESC LIMIT 1",
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
        "date": appt["date"].strftime("%B %d, %Y"),
        "start_time": _format_time(start),
        "end_time": _format_time(end),
        "location": appt["location"],
        "station_no": appt["station_no"],
    }


def get_station_availability(cursor, date_str, location, start_time_str, end_time_str):
    """
    Returns { "Station 1": "vacant" | "reserved", ... } for every station
    in STATION_LIST, based on whether an existing (non-expired) booking on
    `date_str` / `location` overlaps the requested start_time_str/end_time_str
    window. Used to build the checkbox list on the Group Reservation tab so
    already-booked stations can be disabled.

    Overlap rule: an existing appointment conflicts if
    existing.start_time < requested.end_time AND existing.end_time > requested.start_time
    """
    availability = {name: "vacant" for name in STATION_LIST}

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

        if station in availability:
            availability[station] = "reserved"

    return availability


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
    Generates a QR code (in memory) containing the appointment details.

    The payload is compact JSON with short, stable keys so the admin
    scanner can parse it directly (no DB round-trip required just to
    show who/what/where on the scanner's screen), while "id" is what
    /api/checkin actually uses to look up and flip the reservation.
    Keep these key names in sync with whatever the admin system expects.
    """
    qr_payload = {
        "id_number": id_number,  # send this value as-is in the POST body to /api/checkin
        "name": f"{data['first_name']} {data['mi']}. {data['last_name']}",
        "email": data["gsfe_email"],
        "date": data["date"],
        "station": data["station_no"],
        "location": data["location"],
        "start": data["start_time"],
        "end": data["end_time"],
    }

    # error_correction=H (~30% recoverable) so the code still scans
    # cleanly off a phone screen or a slightly smudged printout.
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(json.dumps(qr_payload, separators=(",", ":")))
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def generate_group_station_qr_code(id_number, group_id, group_data, station_no):
    """
    Generates a QR code (in memory) for one station within a group booking.
    Same shape/keys as generate_qr_code() so the existing /api/checkin
    endpoint and admin scanner work unchanged, plus a "group_id" field.
    """
    qr_payload = {
        "id_number": id_number,  # send this value as-is in the POST body to /api/checkin
        "group_id": group_id,
        "name": group_data["group_representative"],
        "email": group_data["gsfe_email"],
        "date": group_data["date"],
        "station": station_no,
        "location": group_data["location"],
        "start": group_data["start_time"],
        "end": group_data["end_time"],
    }

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(json.dumps(qr_payload, separators=(",", ":")))
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
    return render_template("dashboard.html")


# REAL-TIME STATION STATUS (polled by dashboard.html)
@app.route("/api/station-status")
def station_status():
    location = request.args.get("location", LOCATION_LIST[0])

    # Start every station as vacant
    stations = {
        name: {"status": "vacant", "start_time": None, "end_time": None}
        for name in STATION_LIST
    }

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

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
            # Normalize station identifier to match STATION_LIST keys.
            # DB may store station_no as an int (1..10) or as a string like "Station 1".
            raw_station = appt["station_no"]
            if isinstance(raw_station, int):
                station = f"Station {raw_station}"
            elif isinstance(raw_station, str) and raw_station.isdigit():
                station = f"Station {raw_station}"
            else:
                station = raw_station
            if station not in stations:
                # An appointment exists for a station not in STATION_LIST
                # (e.g. STATION_LIST was edited after data was entered) - skip it.
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


# GROUP RESERVATION — STATION AVAILABILITY (polled by dashboard.html
# whenever the date/location/start/end fields on the Group Reservation
# tab are all filled in, to disable already-booked stations in the
# checkbox list)
@app.route("/api/group-station-availability")
def group_station_availability():
    date_str = request.args.get("date")
    location = request.args.get("location", LOCATION_LIST[0])
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

    # De-duplicate + validate against the known station list
    stations = sorted(set(stations), key=lambda s: STATION_LIST.index(s) if s in STATION_LIST else 999)
    invalid_stations = [s for s in stations if s not in STATION_LIST]
    if invalid_stations:
        return jsonify({
            "success": False,
            "message": f"Unknown station(s): {', '.join(invalid_stations)}",
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

        group_id = generate_group_id(cursor)

        insert_query = """
            INSERT INTO appointments
                (id_number, reservation_type, group_id, group_representative, purpose,
                 gsfe_email, date, station_no, location, start_time, end_time, status)
            VALUES (%s, 'group', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        station_bookings = []
        for station_no in stations:
            id_number = generate_id_number(cursor)
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


# CHECK-IN (called once the admin system / QR scanner reads a student's
# QR code at the station - this is what actually flips a booking from
# "reserved" to "occupied". No admin UI exists yet; this endpoint is the
# hook it will call.)
@app.route("/api/checkin", methods=["POST"])
def checkin():
    data = request.get_json(silent=True) or {}
    id_number = data.get("id_number")

    if not id_number:
        return jsonify({"success": False, "message": "Missing id_number."}), 400

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
            }), 404

        if appt["date"] != today:
            return jsonify({
                "success": False,
                "verification": "invalid",
                "message": "This appointment is not for today.",
                "appointment": _serialize_appointment_for_checkin(appt),
            }), 400

        if appt["status"] == STATUS_EXPIRED:
            return jsonify({
                "success": False,
                "verification": "expired",
                "message": f"This reservation expired after {CHECKIN_GRACE_PERIOD_MINUTES} minutes with no check-in.",
                "appointment": _serialize_appointment_for_checkin(appt),
            }), 400

        if appt["status"] == STATUS_OCCUPIED:
            return jsonify({
                "success": True,
                "verification": "verified",
                "message": "Already checked in.",
                "id_number": id_number,
                "appointment": _serialize_appointment_for_checkin(appt),
            })

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
        return jsonify({"success": False, "message": f"Database error: {db_err}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    app.run(debug=True)