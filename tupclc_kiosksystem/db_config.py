# ==========================================================
# CONFIGURATION FILE
# Edit the values below to match your XAMPP MySQL setup and
# the email account you want to send confirmations from.
# See SETUP_TUTORIAL.md for step-by-step instructions.
# ==========================================================

import os

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
#
# Bug fix: the app password used to be committed here in plain text.
# Set it as an environment variable instead (never commit real
# secrets to the repo):
#   Windows (cmd):        set TUPCLC_EMAIL_PASSWORD=your-app-password
#   Windows (PowerShell):  $env:TUPCLC_EMAIL_PASSWORD="your-app-password"
#   macOS/Linux:            export TUPCLC_EMAIL_PASSWORD=your-app-password
EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "tupclearningcommons@gmail.com",
    "sender_password": os.environ.get("TUPCLC_EMAIL_PASSWORD", ""),
}