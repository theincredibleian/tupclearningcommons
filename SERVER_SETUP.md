# Setting Up a New Laptop as the TUPC Learning Commons Server

This is the full guide for turning a brand-new (or freshly wiped)
laptop into a dedicated machine that hosts both the kiosk and admin
systems full-time. Assumes Windows 10/11 (matches the original
installation PDF); a Linux-based alternative is noted at the end for
anyone comfortable with it.

---

## Part 1: Hardware Requirements

This is a lightweight setup (Flask + MySQL for one Learning Commons),
so you don't need anything powerful — but it does need to be reliable
since it'll be running non-stop.

| Component | Minimum | Recommended |
|---|---|---|
| CPU | Intel Core i3 (2015+) or equivalent | Core i5 or better |
| RAM | 4 GB | 8 GB |
| Storage | 128 GB | 256 GB SSD |
| Network | WiFi | Wired Ethernet (more stable for something always-on) |
| Power | — | Can stay plugged in permanently (a laptop that's fine being charged 24/7, or a desktop) |

A laptop that's meant to sit in one place and stay on all day is
effectively acting as a desktop — Ethernet is worth it if the room has
a port available, since it won't drop the way WiFi occasionally does.

## Part 2: Operating System

Windows 10/11 is recommended here since it matches the existing
installation PDF and setup tutorial exactly, and needs no extra
translation of steps. If the laptop doesn't already have Windows, do
a normal fresh installation and finish Windows setup (create a local
account, connect to WiFi, let it install updates) before continuing.

> Skip ahead to **Part 8** if you'd rather use Linux instead — the
> Windows-specific parts are Parts 3–7.

## Part 3: Install Python 3.9

1. Download: https://www.python.org/ftp/python/3.9.0/python-3.9.0-amd64.exe
2. Run the installer.
3. **Tick "Add Python to PATH"** — this is important, skipping it
   causes problems later.
4. Click **Install Now**.
5. Verify it worked — open Command Prompt and run:
   ```
   python --version
   python -m pip --version
   ```
   Both should print a version number, not an error.

> Make sure no other Python version is already installed — multiple
> versions on PATH can cause the wrong one to run.

## Part 4: Install XAMPP (Apache + MySQL)

1. Download: https://www.apachefriends.org/download.html
2. Run the installer, keep the install path as `C:\xampp`.
3. Finish the install and open **XAMPP Control Panel**.
4. Click **Start** next to **Apache** and **MySQL** — both should turn
   green.

## Part 5: Install the Python Packages

Open Command Prompt as administrator and run:

```
python -m pip install flask
python -m pip install mysql-connector-python
python -m pip install qrcode[pil]
```

Or, once you've copied the project files onto this laptop (Part 6),
you can instead run this from inside each project folder:

```
pip install -r requirements.txt
```

## Part 6: Get the Project Files onto This Laptop

Copy the project folder (`tupclc_kiosksystem`, `tupclc_adminsystem`,
`learning_commons_db.sql`, etc.) onto this laptop — by USB drive,
shared Google Drive folder, or `git clone` if it's in a repository.
A reasonable place to put it: `C:\TUPC-LearningCommons\`.

## Part 7: Set Up the Database

1. With XAMPP's MySQL running, open **phpMyAdmin** (the "Admin"
   button next to MySQL in XAMPP Control Panel).
2. Click **New** → **Import** → **Choose File** → select
   `learning_commons_db.sql` (inside `tupclc_adminsystem/`) →
   **Import**.
3. Confirm the `learning_commons_db` database now shows up with its
   tables (`appointments`, `restrictions`, `locations`, `stations`).

---

## Part 8 (Linux alternative): Ubuntu Server Setup

If you'd rather use Linux instead of Windows for better long-term
stability:

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv mysql-server

sudo mysql_secure_installation   # set a MySQL root password when prompted

sudo mysql -u root -p < /path/to/learning_commons_db.sql

cd /path/to/tupclc_kiosksystem
pip3 install -r requirements.txt
cd ../tupclc_adminsystem
pip3 install -r requirements.txt
```

Update `db_config.py` in both folders with the MySQL root password you
set. Everything else below (env vars, running, auto-start) has a
Linux equivalent — ask if you want the `systemd` version instead of
the Windows Task Scheduler steps.

---

## Part 9: Set the Environment Variables (Do This Every Time / Permanently)

Rather than typing these into a terminal every time (which only lasts
that session), set them **permanently** on this machine since it's a
dedicated server:

1. Press `Win`, search **"Environment Variables"**, open
   **"Edit the system environment variables"**.
2. Click **Environment Variables...**
3. Under **User variables**, click **New...** and add each of these:

| Variable name | Value |
|---|---|
| `TUPCLC_EMAIL_PASSWORD` | your Gmail App Password (see below) |
| `TUPCLC_ADMIN_PASSWORD` | a real admin password (don't leave it as `1234`) |
| `TUPCLC_SECRET_KEY` | any long random string, e.g. generate one below |

To generate a random secret key, run this once in Command Prompt:

```
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output as the value for `TUPCLC_SECRET_KEY`.

4. Click **OK** on all the dialogs.
5. **Restart Command Prompt / VS Code** (and reboot to be safe) so the
   new variables actually take effect.

### Getting a Gmail App Password

1. Go to your Google Account → **Security**.
2. Turn on **2-Step Verification** if it isn't on already.
3. Go to **App Passwords** → create one for "Mail" → copy the
   16-character password.
4. Use that as `TUPCLC_EMAIL_PASSWORD` above.

## Part 10: First Manual Run (Test Before Automating)

Before setting up auto-start, confirm everything works manually first:

1. Make sure XAMPP's Apache + MySQL are running.
2. Open Command Prompt:
   ```
   cd C:\TUPC-LearningCommons\tupclc_kiosksystem
   python app.py
   ```
   You should see `Running on http://0.0.0.0:5000`.
3. Open a **second** Command Prompt window:
   ```
   cd C:\TUPC-LearningCommons\tupclc_adminsystem
   python app.py
   ```
   You should see `Running on http://0.0.0.0:5001`.
4. From this same laptop's browser, check:
   - http://127.0.0.1:5000 (kiosk)
   - http://127.0.0.1:5001 (admin — should show the login page)
5. From another device on the same WiFi, find this laptop's IP
   (`ipconfig` → IPv4 Address) and check
   `http://<that-ip>:5000` and `:5001` load there too.

If a Windows Firewall prompt appears the first time you run `app.py`,
click **Allow access** (both Private and Public networks).

Once confirmed working, close both with `Ctrl+C` in each window and
move on to making this automatic.

## Part 11: Make It Start Automatically (So You Don't Have to Manually Run It)

Since this laptop is a dedicated server, you'll want the apps to start
by themselves on boot instead of manually opening two Command Prompt
windows every day.

**Easiest approach: a startup batch script + Task Scheduler.**

1. Create a file `C:\TUPC-LearningCommons\start_kiosk.bat` containing:
   ```bat
   @echo off
   cd /d C:\TUPC-LearningCommons\tupclc_kiosksystem
   python app.py
   ```

2. Create a second file `C:\TUPC-LearningCommons\start_admin.bat`:
   ```bat
   @echo off
   cd /d C:\TUPC-LearningCommons\tupclc_adminsystem
   python app.py
   ```

3. Open **Task Scheduler** (search in the Start menu).
4. Click **Create Task...** (not "Create Basic Task" — the full
   dialog gives more control).
5. **General tab:**
   - Name: `TUPC LC Kiosk`
   - Check **"Run whether user is logged on or not"**
   - Check **"Run with highest privileges"**
6. **Triggers tab** → **New...** → **Begin the task: At startup** → OK.
7. **Actions tab** → **New...** → Action: **Start a program** →
   Program/script: `C:\TUPC-LearningCommons\start_kiosk.bat` → OK.
8. Click **OK**, enter the Windows account password if prompted.
9. Repeat steps 4–8 for a second task named `TUPC LC Admin`, pointing
   to `start_admin.bat` instead.
10. Also make **XAMPP's Apache and MySQL start automatically**: open
    XAMPP Control Panel → tick the checkboxes next to Apache and MySQL
    in the leftmost column (labeled "Svc") so they run as Windows
    services and start on boot without you opening XAMPP manually.

11. **Restart the laptop** to test. Give it a minute after boot, then
    check `http://127.0.0.1:5000` and `:5001` load without you having
    done anything manually.

> Environment variables set in Part 9 as **User variables** are tied
> to your Windows account — if the scheduled task runs under a
> different account, or "run whether user is logged on or not" causes
> it to run outside your session, the app may not see them. If the
> apps fail to send email or the admin password reverts to the
> default `1234` after a reboot, switch those to **System variables**
> instead of User variables in the same Environment Variables dialog.

## Part 12: Keep It Reliable

A few practices worth setting up on a dedicated server machine:

- **Disable sleep/hibernate:** Settings → System → Power → set
  "Screen and sleep" to Never while plugged in. A sleeping laptop
  stops answering requests.
- **Disable automatic Windows updates during operating hours**, or at
  least set an "active hours" window so it doesn't reboot mid-day.
- **Back up `learning_commons_db.sql` periodically** — export the
  database from phpMyAdmin (Export tab) every so often so you have a
  recent copy if something goes wrong.
- **Label the laptop** — "Do not close/unplug/shut down — TUPC LC
  Server" taped to it saves a lot of confusion.

---

## Quick Reference

| What | Where |
|---|---|
| Kiosk system | `http://<server-ip>:5000` |
| Admin system | `http://<server-ip>:5001` |
| Database | phpMyAdmin at `http://<server-ip>/phpmyadmin` (if accessed locally on the server) |
| Admin login password | Whatever you set as `TUPCLC_ADMIN_PASSWORD` |
| To stop an app manually | Find its Command Prompt window (or `Ctrl+C`), or End Task in Task Manager if running via Task Scheduler |
| To restart everything | Reboot the laptop — Task Scheduler + XAMPP services bring it back up |
