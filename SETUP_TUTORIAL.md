# TUPC Learning Commons — Setup Tutorial

This covers the same installation steps as `TUPC-LC Requirements
Installation.pdf`, plus the extra one-time setup needed after the
recent bug fixes (environment variables for secrets, and the new
admin login).

## 1. Install the requirements

1. **Python 3.9** — https://www.python.org/ftp/python/3.9.0/python-3.9.0-amd64.exe
   Tick **"Add Python to PATH"** during install.
2. **Flask** — `python -m pip install flask`
3. **MySQL Connector** — `python -m pip install mysql-connector-python`
4. **QR Code Library** — `pip install qrcode[pil]`
5. **Visual Studio Code** (optional but recommended) —
   https://code.visualstudio.com/
6. **XAMPP** — https://www.apachefriends.org/download.html
   Keep the install path as `C:\xampp`.
7. **Python VS Code Extension** — search "Python" (by Microsoft) in the
   VS Code Extensions tab and install it.

Or, faster: from inside each of `tupclc_kiosksystem/` and
`tupclc_adminsystem/`, run:

```
pip install -r requirements.txt
```

## 2. Get the system files

Open the two folders (`tupclc_kiosksystem`, `tupclc_adminsystem`) in
VS Code — one window each (**File → New Window**) if you want both
running side by side.

## 3. Set up the database

1. Open XAMPP → start **Apache** and **MySQL**.
2. Open **phpMyAdmin** ("Admin" button next to MySQL).
3. Click **New** → **Import** → choose `learning_commons_db.sql`
   (in `tupclc_adminsystem/`) → **Import**.

This creates the `learning_commons_db` database with the
`appointments`, `restrictions`, `locations`, and `stations` tables.

> **Run the admin system at least once before the kiosk system.**
> The kiosk now checks the `stations` table (to respect stations
> closed via Manage Stations), and that table is only auto-created/
> seeded by the admin system's `ensure_schema()` the first time it
> talks to the database.

## 4. Configure secrets (environment variables)

Passwords and secret keys are **no longer stored in `db_config.py`**
— they're read from environment variables instead, so nothing
sensitive gets committed to the repo.

Set these before running either system:

| Variable | Used by | Required? |
|---|---|---|
| `TUPCLC_EMAIL_PASSWORD` | both | Yes — confirmation emails won't send without it |
| `TUPCLC_ADMIN_PASSWORD` | admin only | Recommended — defaults to `1234` if unset |
| `TUPCLC_SECRET_KEY` | admin only | Recommended — if unset, everyone is logged out each time the server restarts |

**Windows (cmd):**
```
set TUPCLC_EMAIL_PASSWORD=your-gmail-app-password
set TUPCLC_ADMIN_PASSWORD=choose-a-real-password
set TUPCLC_SECRET_KEY=some-long-random-string
```

**Windows (PowerShell):**
```
$env:TUPCLC_EMAIL_PASSWORD="your-gmail-app-password"
$env:TUPCLC_ADMIN_PASSWORD="choose-a-real-password"
$env:TUPCLC_SECRET_KEY="some-long-random-string"
```

**macOS/Linux:**
```
export TUPCLC_EMAIL_PASSWORD=your-gmail-app-password
export TUPCLC_ADMIN_PASSWORD=choose-a-real-password
export TUPCLC_SECRET_KEY=some-long-random-string
```

These only last for the current terminal session — set them again
next time you open a new terminal, or add them to your system's
environment variables so they're always there.

### Getting a Gmail App Password

Regular Gmail passwords don't work for SMTP. You need an **App
Password**:

1. Go to your Google Account → **Security**.
2. Turn on **2-Step Verification** if it isn't already on.
3. Go to **App passwords**, create one for "Mail", and copy the
   16-character password it gives you.
4. Use that as `TUPCLC_EMAIL_PASSWORD`.

> If you're reusing this project from before the fix: the old app
> password that used to be hardcoded in `db_config.py` should be
> treated as compromised (it was committed to the repo). Revoke it
> in Google Account → Security → App Passwords, and generate a new
> one.

## 5. Run the systems

1. In XAMPP, make sure **Apache** and **MySQL** are running.
2. In VS Code, open `app.py` in the folder you want to run and use
   the "Run Python File" button, or in the terminal:
   ```
   flask --app app.py --debug run
   ```
3. Open http://127.0.0.1:5000 in your browser.

Only one `app.py` can run per terminal — to switch, press `Ctrl+C`
to stop it, `cd` into the other folder, and run the command again.
Run kiosk and admin in two separate terminals if you want both up
at once (they use the same port by default, so run them on
different machines/ports in a real deployment).

## 5b. Making it reachable from other devices on the same WiFi (LAN)

By default Flask only listens on `127.0.0.1` — only the host machine
itself can open it, even other devices on the same WiFi can't. Both
`app.py` files now bind to `host="0.0.0.0"` instead, which opens them
up to the whole LAN. Kiosk uses port **5000**, admin uses port
**5001** (so both can run on the same laptop at once).

1. Make sure the host laptop and the tablet/other device are on the
   **same WiFi network**.
2. On the host laptop, find its local IP address:
   - Windows: open Command Prompt, run `ipconfig`, look for
     "IPv4 Address" (e.g. `192.168.1.42`).
   - macOS/Linux: run `ifconfig` or `ip addr`.
3. Set the environment variables from Section 4 in the terminal
   before running each app (env vars only last for that terminal
   session — set them again for each new terminal/app).
4. Start XAMPP (Apache + MySQL), then run each app:
   ```
   cd tupclc_kiosksystem
   python app.py
   ```
   ```
   cd tupclc_adminsystem
   python app.py
   ```
5. From the tablet's browser, go to:
   - Kiosk: `http://<laptop-ip>:5000`
   - Admin: `http://<laptop-ip>:5001`

   e.g. `http://192.168.1.42:5000`

6. If it doesn't load, a Windows Firewall prompt may be blocking it —
   allow access for Python when prompted (both Private and Public
   networks), or add a manual firewall rule if the prompt didn't
   appear.

> This only works while both devices are on the same WiFi and the
> host laptop is on and running the apps. It won't work from outside
> that WiFi network (e.g. from home) — that needs either a VPN
> (recommended) or hosting the app on a cloud server instead, which
> is a separate setup.

## 6. Log in to the admin system

The admin system now requires a password before showing any page
(previously it had none — this was one of the bugs fixed).

- **Password:** whatever you set as `TUPCLC_ADMIN_PASSWORD`
  (defaults to `1234` if you didn't set one — change this before
  using the system for real).
- Use **Log Out** in the navbar to end the session.

The kiosk dashboard (the one students use to book a station) does
**not** require a login — it's meant to be public-facing.

## Notes

- Some features (QR check-in) require QR code scanner hardware.
- `qr_payload` / QR codes generated on one machine can be scanned by
  either system's `/api/checkin` endpoint, since both point at the
  same database.
- Debug mode is now off by default (`debug=False` in `app.py`) so
  error pages no longer show full stack traces / internal details.
  Use `flask --app app.py --debug run` only while developing, never
  for a real deployment.
