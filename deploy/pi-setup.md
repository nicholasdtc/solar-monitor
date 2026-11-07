# Setting up the Pi

Takes a fresh Raspberry Pi 5 to the app running as a service, then makes it reachable from away
through Tailscale and hardens it. Replace `<pi-name>` and `<pi-user>` with what you choose in step 1.
Don't commit real addresses to this repo.

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
- `DASHBOARD_USER` and `DASHBOARD_PASSWORD`: the dashboard's login, required if `BIND_HOST` is `0.0.0.0`.
  The app won't start without them, and `journalctl` will say why. Store a hash of the password rather
  than the password itself. This asks for it twice and prints the line to paste in:
  `sudo /opt/solar-monitor/.venv/bin/python -m solar_monitor.password`.
  Use a password you don't use anywhere else, because at home it isn't encrypted on the way across your network.
- `DB_PATH`: leave blank. The service sets it to `/var/lib/solar-monitor/solar-monitor.db`.

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

## 8. Give it its own name (on the Pi)

This puts the dashboard at http://solar.local, with no port. Skip it if `BIND_HOST` is blank.
Make sure the router reserves the Pi's address first (step 2), because the name is announced at
the address the Pi has when it starts.

The app's name is announced alongside the Pi's own one, and Caddy passes requests on port 80 on to
the app, whatever name they use, so the Tailscale name in step 10 works too.
Keep `BIND_HOST=0.0.0.0`: it's what makes the app refuse to start without a login, and the app
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

## 10. Reach it from away with Tailscale

Tailscale connects your own devices to each other, encrypted, wherever they are. Nothing on the Pi is
opened to the internet, and the router needs no port forwarding. From away, the dashboard works as it
does at home, changing settings included, behind the same login.

In the Tailscale admin console (https://login.tailscale.com), on the PC:

- Sign up with an account that has two-factor login turned on. Tailscale relies on it to know it's you.
- Under Settings, Device management, turn on Device approval, so a new device can't join until you approve it.
- Under Access controls, replace the policy with the contents of `deploy/tailscale-policy.hujson` and save.
  It lets your own devices reach SSH and the dashboard on the Pi, and nothing else.
- Install Tailscale on your phone, log in, and approve it.

On the Pi. Raspberry Pi OS is based on Debian, and these lines are for Debian 13, "trixie".
`grep CODENAME /etc/os-release` shows yours, and Tailscale's own instructions have the lines for others.

```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg > /dev/null
```

```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
```

```bash
sudo apt update && sudo apt install -y tailscale
```

The tag gives the Pi only what the policy grants it, which is nothing, and means it doesn't need logging
in again every few months. The Pi doesn't need Tailscale's names, so it keeps its own DNS.

```bash
sudo tailscale up --advertise-tags=tag:solar --accept-dns=false
```

Open the link it prints, log in, then approve the Pi in the admin console. Then let Tailscale keep
itself up to date:

```bash
sudo tailscale set --auto-update
```

Don't turn on Tailscale SSH, Funnel, Serve, subnet routes or an exit node. None are needed, and Funnel
would put the dashboard on the internet. Tailscale may ask the router to forward its own port, and the
router may list that. It's expected: that port only answers your own devices.

With Tailscale on, the phone opens the dashboard at `http://<pi-name>/` from anywhere, and SSH works as
`ssh <pi-user>@<pi-name>`. Type the `http://`, or the browser may search for the name instead.
You'll log in once more for the new address.

## 11. Harden the Pi (on the Pi)

### SSH: keys only

Passwords stop working for SSH, so they can't be guessed. Keep your current SSH window open until
a new one has logged in, in case something's wrong.

```bash
sudo install -m 644 /opt/solar-monitor/deploy/ssh-keys-only.conf /etc/ssh/sshd_config.d/00-keys-only.conf
```

```bash
sudo sshd -t && sudo systemctl reload ssh
```

From the PC, in a new window, this should log in and come straight back:

```bash
ssh <pi-user>@<pi-name>.local true
```

And this should say `Permission denied (publickey)` without asking for a password:

```bash
ssh -o PubkeyAuthentication=no <pi-user>@<pi-name>.local
```

### The firewall

Only the home network and your Tailscale devices can reach SSH and the dashboard, and anything else
that wasn't asked for gets no answer. It only touches its own rules, so Tailscale's are left alone.

```bash
sudo apt install -y nftables
```

Debian's own `nftables.service` clears every rule when it starts or stops, this firewall's and
Tailscale's included, so it must stay off. This should say `disabled`:

```bash
systemctl is-enabled nftables
```

If it says `enabled`, run `sudo systemctl disable --now nftables`.

```bash
sudo install -D -m 644 /opt/solar-monitor/deploy/firewall.nft /etc/solar-monitor/firewall.nft
```

First set it to be removed again in 5 minutes, so a mistake can't lock you out. Then load it.

```bash
sudo systemd-run --collect --on-active=5min --unit=firewall-undo /usr/sbin/nft delete table inet solar_firewall
```

```bash
sudo nft -f /etc/solar-monitor/firewall.nft
```

Within the 5 minutes, check from the PC in a new window that `ssh <pi-user>@<pi-name>.local true`
still works and that http://solar.local opens, and from the phone that `http://<pi-name>/` opens over
Tailscale. Then cancel the removal:

```bash
sudo systemctl stop firewall-undo.timer
```

If that says the unit isn't loaded, the 5 minutes were up and the rules are gone. Run both commands
above again and be quicker. If something didn't work, just wait the 5 minutes, and ask for help
before trying again. Once it's all working, have it load at every boot:

```bash
sudo cp /opt/solar-monitor/deploy/solar-monitor-firewall.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now solar-monitor-firewall
```

At home, a computer may try the Pi's public IPv6 address first. The firewall turns that away at once,
and SSH and browsers go straight on to an address that works, so a line like
`connect to address ... port 22: Connection refused` just before SSH logs in is expected.

### Switch off what isn't used

rpcbind is for sharing folders over NFS, which the Pi doesn't do.

```bash
sudo systemctl mask --now rpcbind.service rpcbind.socket
```

Then list what's listening:

```bash
sudo ss -tulpn
```

Expect SSH on 22, Caddy on 80, the app on 8000, avahi on 5353, and Tailscale. Anything on `127.0.0.1`
or `[::1]` only is fine, since only the Pi can reach it.

### Security updates

```bash
sudo apt install -y unattended-upgrades
```

```bash
cat /etc/apt/apt.conf.d/20auto-upgrades
```

Both lines should end in `"1";`. If not, run `sudo dpkg-reconfigure -plow unattended-upgrades` and
answer Yes. This installs Debian's security updates every day. Tailscale updates itself from step 10.
For everything else, including the Pi's own kernel and firmware, run
`sudo apt update && sudo apt full-upgrade` now and then, and reboot afterwards.

### The dashboard password

If `DASHBOARD_PASSWORD` in `.env` is still the password itself, the app says so in its journal when
it starts. Replace it with a hash, made as in step 5, then restart the app:

```bash
sudo /opt/solar-monitor/.venv/bin/python -m solar_monitor.password
```

```bash
sudo nano /opt/solar-monitor/.env
```

```bash
sudo systemctl restart solar-monitor
```

After 5 wrong logins within 15 minutes, every login is refused for 15 minutes, the right one included,
so guessing gets nowhere. Every login is logged with the address it came from:

```bash
journalctl -u solar-monitor | grep Login
```

## 12. Check it from outside

- Reboot with `sudo reboot`. Afterwards `sudo nft list tables` should list `inet solar_firewall` alongside
  Tailscale's tables, `tailscale status` should list your phone, and the dashboard should open at home.
- On the phone, turn Wi-Fi off. With Tailscale on, `http://<pi-name>/` should open the dashboard.
  With Tailscale off, it shouldn't open at all.
- Still on mobile data with Tailscale off, if your phone has IPv6, try the Pi's public IPv6 address,
  the one `ip -6 addr show scope global` on the Pi lists that doesn't start with `fd`. It shouldn't open.

## If you're locked out

- The dashboard: wait the 15 minutes, or restart the app with `sudo systemctl restart solar-monitor`.
- The firewall: `sudo systemctl stop solar-monitor-firewall` removes it until the next boot, and
  `sudo systemctl disable solar-monitor-firewall` stops it loading at boot.
- SSH: if you lose the PC's key, plug a keyboard and screen into the Pi and log in there with your
  password. Neither the SSH settings nor the firewall affect that. Then add a new key to
  `~/.ssh/authorized_keys`.

## Updating to a new version

```bash
sudo git -C /opt/solar-monitor pull && sudo systemctl restart solar-monitor
```

If `pyproject.toml` or the service file changed, repeat the install command from step 4 or the
copy and `daemon-reload` from step 7 before restarting. Likewise, if the `Caddyfile` or
`solar-monitor-name.service` changed, repeat their commands from step 8, then
`sudo systemctl restart solar-monitor-name`. A restart saves unsaved readings first,
but a power cut can lose up to 5 minutes of them.

The files from steps 10 and 11 change rarely, and none of them take effect until they're installed again:

- `ssh-keys-only.conf`: repeat its `install` command, then `sudo sshd -t && sudo systemctl reload ssh`,
  and check a new SSH window logs in before closing the old one.
- `firewall.nft`: repeat its `install` command and the 5-minute undo, then load it with
  `sudo systemctl reload solar-monitor-firewall`, check as before, and cancel the undo.
- `solar-monitor-firewall.service`: repeat its `cp` command, then `sudo systemctl daemon-reload`.
- `tailscale-policy.hujson`: paste it into the admin console again.
