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
| Collector | Python 3.11+, `goodwe` library (UDP, local) |
| Storage | SQLite (WAL mode) |
| API | FastAPI + uvicorn |
| UI | Vue 3 + Vite, built on the PC, static files served by FastAPI |
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
    api.py               # FastAPI: /api/live, /api/history, /api/daily
    main.py              # runs collector + API
  web/                   # Vue app (Vite)
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
- Confirm the inverter model and family first. Install notes mention a Solis inverter, but GoodWe ESA batteries need a GoodWe inverter. If it is really Solis, this library does not apply, so stop and re-plan.
- Check firmware: old ARM firmware can block UDP, and GoodWe support must upgrade it.

**Phase 2: collector + SQLite**
- Poll every 10-30s, buffer in memory, flush to SQLite every ~5 minutes (SD card wear).
- Tables: `samples` (timestamp, pv_w, load_w, battery_w, battery_soc, grid_w, raw JSON) and `daily_counters`.
- Poll counters hourly or daily and store the differences. Gaps are harmless because counters catch up.

**Phase 3: API + dashboard**
- Endpoints: live values, history by range, daily totals.
- Vue dashboard: live tiles, power curves, daily totals, battery SOC over time.
- Build with `npm run build` on the PC. The Pi serves static files only, with no Node on the Pi.

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

- Exact inverter model and firmware (label or app).
- Which inverter settings matter most to change (battery charge/discharge schedule, backup reserve).
- Any retention policy for raw samples (for example, downsample after 90 days).