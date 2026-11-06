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
| 5 | Remote access via Tailscale, nightly backup to the PC | 5 |
| 6 | Control panel: battery schedule / settings writes (auth required) | 6 |

## Hard rules

- **Read-only first.** No writes to the inverter until phase 6. A wrong write to a battery schedule is harder to undo than a missing data point.
- **Never expose the inverter or the app to the public internet.** The inverter's local protocol has no authentication. Remote access is VPN-only (Tailscale).
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
| Remote | Tailscale |

## Repo layout

```
solar-monitor/
  SPEC.md
  CLAUDE.md              # short notes for Claude Code (copy key rules from here)
  .env.example
  .gitignore
  pyproject.toml
  src/solar_monitor/
    config.py            # loads .env
    collector.py         # poll loop: read_runtime_data -> buffer -> batched SQLite writes
    db.py                # schema, migrations, queries
    api.py               # FastAPI: /api/latest, /api/history, /api/daily
    dashboard.html       # the dashboard page
    main.py              # runs collector + API
  scripts/probe.py       # one-off read of the inverter
  deploy/
    solar-monitor.service
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

**Phase 4: deploy to the Pi**
- Reflash the card with Raspberry Pi Imager: hostname, user, SSH key, SSH enabled. Remove the SD card before fitting the Pi in the case.
- Reserve fixed IPs for the Pi and the inverter in the router. Use a wired connection.
- Copy the code, create a venv, run as a `systemd` service (`Restart=always`).

**Phase 5: remote access + backup**
- Install Tailscale on the Pi, PC and phone. Access the dashboard at `http://<pi-name>:<port>`.
- Nightly cron on the Pi runs `sqlite3 .backup` (never a plain file copy while the collector writes). The PC pulls the backup via scp/rsync, with a Windows scheduled task. A missed night just catches up.
- Keep the config and service files in git. Write the restore steps in `deploy/restore.md`: flash a new card, reinstall, copy back config and database, enable the service.

**Phase 6: control (optional)**
- Battery schedule and settings writes behind login, reachable over the VPN only.
- Log every write. Show the current value before changing it. Make it easy to revert.

## Open questions

- Which inverter settings matter most to change (battery charge/discharge schedule, backup reserve).
- Any retention policy for raw samples (for example, downsample after 90 days). At a 15s poll they grow by about 7 MB a day, or 2.4 GB a year.