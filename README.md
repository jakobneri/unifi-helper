# unifi-restart

Restart your **entire UniFi network** — every switch, access point, and the
gateway/UDM — on a daily schedule, driven from a device that lives on that
same network (e.g. a Raspberry Pi). Talks directly to the UniFi Network API
— no cloud, no UniFi account required.

---

## Important: the controlling device goes offline too

This tool is meant to run on a machine (like a Raspberry Pi) that is itself
connected *through* one of the UniFi devices it restarts — typically a
switch or AP port. That means **the Pi will briefly lose network
connectivity during every run**, right along with everything else.

To handle that:

- Restart commands are **fire-and-forget**. The tool sends a `restart` API
  call per device and moves on immediately — it never waits for a device to
  come back online (it can't; its own link may already be down).
- The **gateway/UDM is restarted last**. It usually sits upstream of
  switches and APs, so restarting it first would cut connectivity to the
  rest of the network before their commands even go out.
- Errors partway through a run (typically starting with whichever device
  carries the Pi's own uplink) are **expected**, not a sign the tool is
  broken. They're logged, and the run continues attempting the remaining
  devices for as long as the connection survives.
- Everything is logged locally to `~/.local/state/unifi-restart/restart.log`
  so you can check what happened after the Pi comes back online, even
  though nothing could reach you over the network during the run itself.

---

## Requirements

- Python 3.8+
- UniFi Network application running on the same machine (or reachable on your LAN)
- `systemd` (default on Raspberry Pi OS / most Debian-based distros) for the daily timer
- Optional: `tabulate` for prettier `list` output

---

## Installation

> **Debian / Ubuntu / Raspberry Pi OS**: system Python is protected (PEP 668).
> Use a virtual environment — the commands below handle that automatically.

```bash
git clone https://github.com/jakobneri/unify-led.git
cd unify-led

# create a virtual environment and install
python3 -m venv ~/.venv/unifi-restart
source ~/.venv/unifi-restart/bin/activate
pip install -e .

# optional: prettier tables
pip install tabulate
```

The `unifi-restart` command is now available **while the venv is active**.

**Activate the venv in future terminal sessions:**

```bash
source ~/.venv/unifi-restart/bin/activate
```

Or add that line to your `~/.bashrc` so it activates automatically on login:

```bash
echo 'source ~/.venv/unifi-restart/bin/activate' >> ~/.bashrc
```

You can also run without activating the venv:

```bash
~/.venv/unifi-restart/bin/unifi-restart --help
```

> The `install-timer` command (below) looks for the tool at
> `~/.venv/unifi-restart/bin/unifi-restart` first, so installing into that
> exact path avoids any extra configuration.

---

## Step-by-step setup

### 1. Run the configuration wizard

```bash
unifi-restart config
```

You will be prompted for:

| Field | Example | Default |
|-------|---------|---------|
| Mode | `standalone` or `unifi-os` | `unifi-os` |
| Host | `https://192.168.1.1` | `https://localhost` |
| Username | `admin` | `admin` |
| Password | *(your UniFi password)* | — |
| Site | `default` | `default` |

Settings are saved to `~/.config/unifi-restart/config.json` (mode 600).
The session cookie is cached in `~/.config/unifi-restart/session.json` so
you only log in once.

> **Self-signed certificate**: UniFi uses a self-signed cert by default. The tool skips TLS verification automatically — no extra steps needed.

---

### 2. List your devices

Check which devices the tool sees (and in what order they'll be restarted):

```bash
unifi-restart list
```

Example output:

```
Name           MAC                Model     Type
-------------  -----------------  --------  ----
living-room    aa:bb:cc:dd:ee:ff  U6-Pro    uap
office-switch  11:22:33:44:55:66  USW-Flex  usw
gateway        99:88:77:66:55:44  UDM-Pro   udm
```

---

### 3. Do a dry run

See the exact restart order without sending any commands (gateway/UDM always last):

```bash
unifi-restart run --dry-run
```

---

### 4. Restart everything now

```bash
unifi-restart run
```

Add `--hard` to power-cycle PoE devices instead of a soft reboot:

```bash
unifi-restart run --hard
```

---

### 5. Install the daily 3am timer

```bash
sudo unifi-restart install-timer
```

This installs and enables a `systemd` service + timer
(`/etc/systemd/system/unifi-restart.{service,timer}`) that runs
`unifi-restart run` every day at **03:00**, as the user who invoked `sudo`
(so it picks up that user's config).

Choose a different time or a hard restart:

```bash
sudo unifi-restart install-timer --time 03:30 --hard
```

Remove the timer:

```bash
sudo unifi-restart install-timer --remove
```

---

### 6. Check status

```bash
unifi-restart status
```

Shows the timer's next scheduled run plus the last few log entries:

```
NEXT                        LEFT     LAST                         PASSED  UNIT                 ACTIVATES
Mon 2026-07-07 03:00:00 UTC 8h left  Sun 2026-07-06 03:00:00 UTC   16h ago unifi-restart.timer  unifi-restart.service

Last log entries (~/.local/state/unifi-restart/restart.log):
  2026-07-06 03:00:01 INFO Starting full network restart (3 devices, hard=False)
  2026-07-06 03:00:01 INFO living-room: restart command sent
  2026-07-06 03:00:02 INFO office-switch: restart command sent
  2026-07-06 03:00:02 WARNING gateway: error — HTTPSConnectionPool(...)
  2026-07-06 03:00:02 INFO Restart sequence complete (device reboots happen asynchronously)
```

---

## Command reference

```
unifi-restart config                          # interactive setup wizard
unifi-restart list                            # list all devices and restart order
unifi-restart run [--dry-run] [--hard]        # restart every device now
unifi-restart install-timer [--time HH:MM] [--hard] [--remove]  # manage the daily systemd timer
unifi-restart status                          # show timer status + recent log entries
```

---

## Config & state file locations

| File | Purpose |
|------|---------|
| `~/.config/unifi-restart/config.json` | Host, credentials, site |
| `~/.config/unifi-restart/session.json` | Cached session cookie + CSRF token |
| `~/.local/state/unifi-restart/restart.log` | Log of every restart run |
| `/etc/systemd/system/unifi-restart.service` | Installed by `install-timer` |
| `/etc/systemd/system/unifi-restart.timer` | Installed by `install-timer` |
