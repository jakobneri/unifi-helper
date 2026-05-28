# unifi-led

Control the LED override state and schedule of UniFi devices from the command line. Talks directly to the UniFi Network API running on localhost — no cloud, no UniFi account required.

---

## Requirements

- Python 3.8+
- UniFi Network application running on the same machine (or reachable on your LAN)
- Optional: `tabulate` for prettier output (see below)

---

## Installation

> **Debian / Ubuntu / Raspberry Pi OS**: system Python is protected (PEP 668).  
> Use a virtual environment — the commands below handle that automatically.

```bash
git clone https://github.com/jakobneri/unify-led.git
cd unify-led

# create a virtual environment and install
python3 -m venv ~/.venv/unifi-led
source ~/.venv/unifi-led/bin/activate
pip install -e .

# optional: prettier tables
pip install tabulate
```

The `unifi-led` command is now available **while the venv is active**.

**Activate the venv in future terminal sessions:**

```bash
source ~/.venv/unifi-led/bin/activate
```

Or add that line to your `~/.bashrc` so it activates automatically on login:

```bash
echo 'source ~/.venv/unifi-led/bin/activate' >> ~/.bashrc
```

You can also run without activating the venv:

```bash
~/.venv/unifi-led/bin/unifi-led --help
```

---

## Step-by-step setup

### 1. Run the configuration wizard

```bash
unifi-led config
```

You will be prompted for:

| Field | Example | Default |
|-------|---------|---------|
| Host | `https://192.168.1.1` | `https://localhost` |
| Username | `admin` | `admin` |
| Password | *(your UniFi password)* | — |
| Site | `default` | `default` |

Settings are saved to `~/.config/unifi-led/config.json` (mode 600).  
The session cookie is cached in `~/.config/unifi-led/session.json` so you only log in once.

> **Self-signed certificate**: UniFi uses a self-signed cert by default. The tool skips TLS verification automatically — no extra steps needed.

---

### 2. List your devices

```bash
unifi-led list
```

Example output:

```
Name           MAC                Model     LED Override
-------------  -----------------  --------  ------------
living-room    aa:bb:cc:dd:ee:ff  U6-Pro    default
office-switch  11:22:33:44:55:66  USW-Flex  off
```

---

### 3. Control LEDs

Turn off a single device by MAC address:

```bash
unifi-led set aa:bb:cc:dd:ee:ff off
```

Turn all devices on:

```bash
unifi-led set all on
```

Restore UniFi's default behaviour (follows the controller's global LED setting):

```bash
unifi-led set all default
```

Valid states: `on`, `off`, `default`

---

### 4. Set a schedule

Have LEDs turn on at 07:00 and off at 22:00 every day:

```bash
unifi-led schedule --on 07:00 --off 22:00
```

This injects two entries into your crontab, marked with `# unifi-led-managed` so they can be cleanly removed later. Your existing crontab is left untouched.

Check what was written:

```bash
crontab -l
```

Remove the schedule:

```bash
unifi-led schedule --remove
```

---

### 5. Check current status

```bash
unifi-led status
```

Shows the active schedule (if any) followed by the LED state of every device:

```
Managed schedule:
  07:00  unifi-led set all on
  22:00  unifi-led set all off

Name           MAC                LED Override
-------------  -----------------  ------------
living-room    aa:bb:cc:dd:ee:ff  default
office-switch  11:22:33:44:55:66  off
```

---

## Command reference

```
unifi-led config                        # interactive setup wizard
unifi-led list                          # list all devices
unifi-led set <mac|all> on|off|default  # set LED override
unifi-led schedule --on HH:MM --off HH:MM  # write crontab schedule
unifi-led schedule --remove             # remove managed crontab entries
unifi-led status                        # show schedule + device states
```

---

## Config file locations

| File | Purpose |
|------|---------|
| `~/.config/unifi-led/config.json` | Host, credentials, site |
| `~/.config/unifi-led/session.json` | Cached session cookie + CSRF token |
