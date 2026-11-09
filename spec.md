# Solar Monitor: project spec

Local monitoring and (later) control for a GoodWe hybrid inverter and battery system.
Runs on a Raspberry Pi 5 (8GB, wired Ethernet). Developed on Windows.

## Goals

| # | Goal | Phase |
|---|------|-------|
| 1 | Log PV, load, battery SOC/power and grid power every 10-30s | 1-2 |
| 2 | Log daily energy counters (import/export, PV yield, battery charge/discharge) | 1-2 |
| 3 | Web dashboard: live values, history, "why was the battery empty at 17:00?" | 3 |
| 4 | Runs 24/7 on the Pi as a systemd service | 4 |
| 5 | Nightly backup to the PC, and remote access via Tailscale only if it's ever wanted | 5 |
| 6 | Control panel: battery schedule / settings writes (auth required) | 6 |
| 7 | Weather forecast on the dashboard and its own tab, groundwork for forecast-driven automation | 7 |

## Hard rules

- **Read-only first.** No writes to the inverter until phase 6. A wrong write to a battery schedule is harder to undo than a missing data point.
- **Never expose the inverter or the app to the public internet.** The inverter's local protocol has no authentication. On the local Wi-Fi no VPN is needed, as long as the app has accounts and the router forwards nothing to it or the inverter. Reaching the app from the internet would require a VPN first (Tailscale, see issue #2).
- **Poll gently** (10-30s). Check the GoodWe app still updates while polling (the Wi-Fi dongle can get overloaded).
- **No secrets in git.** Inverter IP, tokens and passwords live in `.env` (gitignored). Commit `.env.example` only.

## Stack

| Layer | Choice |
|-------|--------|
| Collector | Python 3.11+, `goodwe` library (Modbus TCP on port 502, local) |
| Storage | SQLite (WAL mode) |
| API | FastAPI + uvicorn |
| UI | A single plain HTML page with no build step or dependencies, served by FastAPI |
| Host | Raspberry Pi OS (64-bit), plain systemd (no Docker) |
| Remote | None on the local Wi-Fi. Tailscale if access from the internet is ever wanted |

## Repo layout

```
solar-monitor/
  SPEC.md
  CLAUDE.md              # short notes for Claude Code (copy key rules from here)
  .env.example
  .gitignore
  pyproject.toml
  start-dashboard.bat    # runs the app in a window; closing the window or Ctrl+C saves and stops it
  stop-dashboard.bat     # saves and stops every running copy, including one in the background
  src/solar_monitor/
    config.py            # loads .env
    auth.py              # login page, session cookie and each route's permissions, around the whole app
    accounts.py          # the Users screen's API, and changing your own password
    login.html           # the login page
    collector.py         # poll loop: read_runtime_data -> buffer -> batched SQLite writes
    db.py                # schema, migrations, queries
    planning.py          # battery time remaining, capacity measured from the log, appliance load sums, load planners, tariff sums
    schedule.py          # reads the working mode, mode switches and TOU time periods (never writes)
    control.py           # the only code that writes to the inverter: one TOU period's power, checked before and after
    export.py            # downloadable copies of the app's data: one entry per format
    importer.py          # brings an export back in: checks the whole file first, then only adds what's missing
    inputs.py            # the limits on appliances and settings, shared by the API and the importer
    api.py               # FastAPI: /api/latest, /api/history, /api/daily, /api/appliances, /api/settings, /api/tou, /api/export, /api/import
    dashboard.html       # the dashboard page
    fonts/               # the page's fonts, served locally so it works without internet
    main.py              # runs collector + API; reset-password sets an account's password
    stop.py              # asks every running copy to save and stop, for stop-dashboard.bat
  scripts/probe.py       # one-off read of the inverter
  deploy/
    solar-monitor.service
    pi-setup.md          # flash the card, install, start the service
    backup.sh            # on the Pi: sqlite3 .backup to /var/backups/solar-monitor
    pull-backup.ps1      # on the PC: scp/rsync the latest backup
    restore.md
  tests/
```

## Phases

**Phase 0: scaffold (PC)**
- Create the repo, Python venv, `pyproject.toml`, `.env.example`.
- Native Windows is fine. The `goodwe` library is pure Python. Use a fixed inverter IP (UDP broadcast discovery does not work under WSL2 NAT).

**Phase 1: read-only probe (PC)**
- Script that connects to the inverter IP and prints `read_runtime_data()`.
- Confirmed: a GoodWe GW5K-EHA-G20, `goodwe` family ET. It answers Modbus TCP on port 502, and did not answer UDP on 8899.

**Phase 2: collector + SQLite**
- Poll every 10-30s, buffer in memory, flush to SQLite every ~5 minutes (SD card wear).
- Tables: `samples` (timestamp, pv_w, load_w, battery_w, battery_soc, grid_w, compressed raw JSON) and `daily_counters`.
- `daily_counters` keeps each day's first and last lifetime reading, and daily energy is the difference from the previous recorded day. Gaps are harmless because counters catch up. Each poll already returns the counters, so they need no separate polling.
- Power readings are stored as reported. In particular `grid_w` keeps the inverter's sign, so confirming its direction never needs a data migration.

**Phase 3: API + dashboard**
- Endpoints: live values, history by range, daily totals.
- Dashboard: live tiles, power curves, daily totals, battery SOC over time.
- Kept as one plain page rather than Vue. It needs no Node on the PC or the Pi, and it can still move to Vue later if it outgrows this.
- History is averaged into at most 800 points per range, and battery and grid stay signed as reported, matching the SEMS app.
- Battery time remaining counts down to the inverter's minimum charge while discharging and up to where the inverter will stop while charging, and its label says which. That stop is the running TOU charge period's cut-off or the inverter's overall charge limit, whichever is lower, or 100% if neither can be read; there is no charge target to set in the app, so nothing to keep in step with the inverter. It uses live inverter values wherever they can be trusted: charge level, health, the minimum, reserve and maximum charge settings, and the TOU periods. Capacity isn't on the network, so it's measured from the log, with an optional entered figure until there's enough to measure. Hardware specs that labels would give aren't stored.
- Appliances are entered by each user and live in the database, never in git.
- The Appliances & tariffs tab also plans: the biggest load the battery could carry through a power cut of a chosen length (down to the reserve), and the average load it could carry until a chosen clock time (down to the minimum, ignoring solar and the grid). Both are capped at the user's current limit, and neither stores a hardware rating.
- Tariff rates are entered by each user and live in the database, never in git. The sums use only the rates, so the times each rate applies aren't stored. Round-trip efficiency is a typical figure, because the log can't measure the whole path from the grid through the battery to the house.

**Phase 4: deploy to the Pi**
- Reflash the card with Raspberry Pi Imager: hostname, user, SSH key, SSH enabled. Remove the SD card before fitting the Pi in the case.
- Reserve fixed IPs for the Pi and the inverter in the router. Use a wired connection.
- Copy the code, create a venv, run as a `systemd` service (`Restart=always`). Steps are in `deploy/pi-setup.md`.
- The app listens on localhost unless `BIND_HOST` in `.env` says otherwise. Opening it to the home network is allowed, and requires accounts (see Accounts below), logged in to through the app's own login page. Forwarding it at the router never is: plain HTTP doesn't encrypt the password or the session cookie.

**Phase 5: remote access + backup**
- Only if access from the internet is ever wanted (issue #2): install Tailscale on the Pi, PC and phone, and access the dashboard at `http://<pi-name>:<port>`. It isn't needed on the local Wi-Fi.
- Nightly cron on the Pi runs `sqlite3 .backup` (never a plain file copy while the collector writes). The PC pulls the backup via scp/rsync, with a Windows scheduled task. A missed night just catches up.
- Keep the config and service files in git. Write the restore steps in `deploy/restore.md`: flash a new card, reinstall, copy back config and database, enable the service.

**Phase 6: control (optional)**
- Battery schedule and settings writes behind the login, for accounts allowed to write to the inverter, on the local Wi-Fi with nothing forwarded at the router (see the hard rules and issue #1).
- Log every write. Show the current value before changing it. Make it easy to revert.
- Protecting the equipment comes first. Double check every write before it runs: read the current value again, compare the exact value to be written with what was read, and keep the old value for undo. Read it back afterwards and compare. Start with the smallest harmless change, and leave out any setting that can't be written safely.
- Done so far: the Battery schedule tab shows the settings. TOU has its own on/off switch, separate from the working mode. Don't use the library's `set_operation_mode` for the Eco modes: it overwrites the first time period and disables the others. Each period's on/off byte also marks its type, so write the matching value for that type rather than a plain 0 or 1.
- The same tab changes one thing: a TOU programme's power, through `control.py`. A change has to name the bytes the page was showing, and is refused if the inverter holds anything else. Only the power bytes differ in what's sent, keeping the direction and the type's units. It's logged in `inverter_writes` before sending, with who asked for it, read back byte for byte afterwards, and never retried by itself. Without accounts, only the machine the app runs on can send one.
- From the first real change (50% to 30%, mid-charge): the inverter acted on it straight away, and the older copy of the period at 47515 caught up by itself within about 25 seconds. SEMS+ still showed 50% a few minutes later and showed 30% within about 48 minutes, so it lags and isn't an immediate check on what the inverter holds. Whether saving there while it's behind sends the old value back isn't known.

**Accounts**
- Each person has their own account, kept in the database: a username (lower case, can't be changed) and a salted scrypt hash of a password of at least 8 characters. Sessions are kept as a SHA-256 of the cookie's token, and each request reads the account afresh, so a change of permissions applies at once.
- Permissions are ticks on the Users screen, which only admins see. Admin allows everything. Write to inverter allows changing the battery schedule and settings, and opens the Battery schedule tab. Edit data allows changing appliances, tariffs and planning choices, and applying an import. Each other tab has its own tick; the Dashboard is always open. The server checks every route against a table in `auth.py`, and a route missing from it is for admins only.
- The owner's account is named `admin`. It can't be removed or lose admin, and only the owner can change its password, from the menu. On the machine that runs the app, `solar-monitor reset-password <name>` sets any account's password, and makes the owner's account if there isn't one: that's how accounts start, and how a lost password is recovered.
- Changing your own password logs you out everywhere else. An admin setting someone's password, or removing them, logs them out everywhere.
- Without any accounts there's no login, and only the machine the app runs on can open it, as before.
- The Action history tab lists what each person did: changes to the inverter from its two logs, and data, account and log-in changes from `actions`. Changes of display choices, which are each person's own, aren't listed.
- Exports leave the accounts and sessions out. The full database export deletes them from the copy securely, so no password hash is left in it.

**Phase 7: weather**
- A forecast from Open-Meteo, fetched hourly by the Pi and kept in SQLite. Yr was considered, but it has no solar radiation.
- A Dashboard widget, a Weather tab with the hourly detail, expected solar output learned from the log, and an automation stub that only recommends.
- Details in [weather-spec.md](weather-spec.md).

## Open questions

- Which inverter settings matter most to change (battery charge/discharge schedule, backup reserve).
- Any retention policy for raw samples (for example, downsample after 90 days). At a 15s poll they grow by about 7 MB a day, or 2.4 GB a year.