# Setting up the Pi

Takes a fresh Raspberry Pi 5 to the app running as a service. Replace `<pi-name>` and
`<pi-user>` with what you choose in step 1. Don't commit real addresses to this repo.

## 1. Flash the card (on the PC)

In Raspberry Pi Imager, choose Raspberry Pi OS Lite (64-bit), then in the OS customisation settings:

- Set a hostname and a username.
- Enable SSH with public-key authentication and paste in your PC's public key.
- Set the time zone to your own. Daily energy totals are split at local midnight, so this matters.

Remove the SD card before fitting the Pi in the case. Connect it by Ethernet and power it on.

## 2. Reserve the addresses (in the router)

Give both the Pi and the inverter a fixed IP with a DHCP reservation. Don't add any port forwarding.

## 3. Check you can log in (on the PC)

```bash
ssh <pi-user>@<pi-name>.local
```

## 4. Install the app (on the Pi)

The code is owned by root and the app runs as its own user, so it can't change its own code.

```bash
sudo apt update && sudo apt install -y git python3-venv
```

```bash
sudo useradd --system --home-dir /opt/solar-monitor --shell /usr/sbin/nologin solar-monitor
```

```bash
sudo git clone https://github.com/nicholasdtc/solar-monitor.git /opt/solar-monitor
```

The install has to be `-e`, because the app looks for `.env` relative to its source folder.

```bash
cd /opt/solar-monitor && sudo python3 -m venv .venv && sudo .venv/bin/pip install -e .
```

## 5. Configure it (on the Pi)

```bash
sudo install -m 640 -o root -g solar-monitor /opt/solar-monitor/.env.example /opt/solar-monitor/.env
```

```bash
sudo nano /opt/solar-monitor/.env
```

- `INVERTER_IP`: the inverter's reserved address.
- `BIND_HOST`: `0.0.0.0` to open the dashboard from other devices at home. Leave it blank to keep it
  private to the Pi, and reach it from the PC through SSH instead:
  `ssh -L 8000:localhost:8000 <pi-user>@<pi-name>.local`, then open http://localhost:8000.
- `DB_PATH`: leave blank. The service sets it to `/var/lib/solar-monitor/solar-monitor.db`.

Accounts aren't set in `.env`. With `BIND_HOST=0.0.0.0` the app won't start until the owner's
account exists, which step 7 makes.

## 6. Stop the app on the PC

Only one copy should poll the inverter. Close the PC's copy before starting the Pi's.

## 7. Start the service (on the Pi)

```bash
sudo cp /opt/solar-monitor/deploy/solar-monitor.service /etc/systemd/system/
```

This makes startup wait for the clock to be set. If the internet is down after a power cut, logging
waits until it's back, rather than saving readings with the wrong time.

```bash
sudo systemctl enable --now systemd-time-wait-sync
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now solar-monitor
```

If `BIND_HOST` is `0.0.0.0`, make the owner's account, named `admin`. Until then the service keeps
restarting, and `journalctl` says why. Use a password you don't use anywhere else, because it isn't
encrypted on the way across your network.

```bash
sudo -u solar-monitor DB_PATH=/var/lib/solar-monitor/solar-monitor.db /opt/solar-monitor/.venv/bin/solar-monitor reset-password admin
```

Log in as `admin` and add everyone else from the menu, under Users. The same command, with someone's
username, sets a forgotten password and logs them out everywhere. Run it as the `solar-monitor`
user, as above, so the database stays the app's.

## 8. Give it its own name (on the Pi)

This puts the dashboard at http://solar.local, with no port. Skip it if `BIND_HOST` is blank.
Make sure the router reserves the Pi's address first (step 2), because the name is announced at
the address the Pi has when it starts.

The app's name is announced alongside the Pi's own one, and Caddy passes requests for it on to the app.
Keep `BIND_HOST=0.0.0.0`: it's what makes the app refuse to start without accounts, and the app
can't tell it's behind Caddy. Port 8000 keeps working too, behind the same login.

```bash
sudo apt install -y caddy avahi-utils
```

```bash
sudo cp /opt/solar-monitor/deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

```bash
sudo cp /opt/solar-monitor/deploy/solar-monitor-name.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now solar-monitor-name
```

The login cookie belongs to the address, so you'll log in again the first time you use the new one.

## 9. Check it

```bash
systemctl status solar-monitor
```

```bash
journalctl -u solar-monitor -n 20
```

```bash
timedatectl
```

- The journal should show `Polling ...` and no `Inverter reads failing` line.
- `timedatectl` should show your time zone and `System clock synchronized: yes`.
- Open the dashboard: http://solar.local after step 8, `http://<pi-name>.local:8000` if you set `BIND_HOST=0.0.0.0`, otherwise
  http://localhost:8000 through the SSH command in step 5. The tiles should update every 15 seconds
  and the history should start filling in. Daily totals can take up to 5 minutes to appear.
- Reboot with `sudo reboot` and check it comes back on its own.
- Check the GoodWe app still updates while the Pi polls.

## Updating to a new version

```bash
sudo git -C /opt/solar-monitor pull && sudo systemctl restart solar-monitor
```

If `pyproject.toml` or the service file changed, repeat the install command from step 4 or the
copy and `daemon-reload` from step 7 before restarting. Likewise, if the `Caddyfile` or
`solar-monitor-name.service` changed, repeat their commands from step 8, then
`sudo systemctl restart solar-monitor-name`. A restart saves unsaved readings first,
but a power cut can lose up to 5 minutes of them. Restarts and reboots keep you logged in, because
the app keeps its logins in its database.

The first version with accounts makes the owner's account, `admin`, from `DASHBOARD_PASSWORD` in
`.env`, and logs everyone out. Log in as `admin` with that password, then remove `DASHBOARD_USER`
and `DASHBOARD_PASSWORD` from `.env`: until then, the journal reminds you at every start.
