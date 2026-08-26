# ==========================================================
# CONFIGURATION FILE
# Edit the values below to match your XAMPP MySQL setup and
# the email account you want to send confirmations from.
# See SETUP_TUTORIAL.md for step-by-step instructions.
# ==========================================================

# ---- MySQL / XAMPP database settings ----
DB_CONFIG = {
    "host": "localhost",
    "user": "root",       # default XAMPP MySQL user
    "password": "",       # default XAMPP MySQL password is blank
    "database": "learning_commons_db",
}

# ---- Email settings (used to send the QR code confirmation) ----
# NOTE: For Gmail, you must use an "App Password", not your normal
# Gmail password. See SETUP_TUTORIAL.md, Section 6.
EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "tupclearningcommons@gmail.com",
    "sender_password": "sbtetwdxhdtgiouu",
}