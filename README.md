# unifi-restart

Restart your **entire UniFi network** — every switch, access point, and the
gateway — on a daily schedule, driven from a device that lives on that same
network (e.g. a Raspberry Pi). Devices are rebooted directly over **SSH**
using their local console credentials — no UniFi Network API login, no
Ubiquiti SSO account, nothing that can lock you out.

---

## Why SSH instead of the UniFi Network API

The UniFi Network API requires logging in with a UniFi account. Repeated
failed logins (e.g. while still getting the config right) can get that
account **locked** (`AUTHENTICATION_FAILED_ACCOUNT_LOCKED`) — which is
exactly the kind of thing you don't want a daily automated job to risk.

SSH sidesteps that entirely:

- The **gateway** (e.g. a UDM or UniFi Express) has its own local root
  account, enabled under Settings → System → Advanced/Console access.
- **Switches and APs** accept a shared SSH username/password that UniFi
  pushes out to every adopted device once you set it under
  Settings → System → SSH Authentication ("Device SSH Authentication").

Both are configured once in this tool and used purely to run `reboot` over
SSH — nothing else.

---

## Important: the controlling device goes offline too

This tool is meant to run on a machine (like a Raspberry Pi) that is itself
connected *through* one of the UniFi devices it restarts — typically a
switch or AP port. That means **the Pi will briefly lose network
connectivity during every run**, right along with everything else.

To handle that:

- Reboots are **fire-and-forget**. The tool opens an SSH connection, sends
  `reboot`, and moves on immediately — it never waits for a device to come
  back online (it can't; its own link may already be down).
- The **gateway is restarted last**. It usually sits upstream of switches
  and APs, so restarting it first would cut connectivity to the rest of the
  network before their commands even go out.
- Errors partway through a run (typically starting with whichever device
  carries the Pi's own uplink) are **expected**, not a sign the tool is
  broken. They're logged, and the run continues attempting the remaining
  devices for as long as the connection survives.
- Everything is logged locally to `~/.local/state/unifi-restart/restart.log`
  so you can check what happened after the Pi comes back online, even
  though nothing could reach you over the network during the run itself.

**Exception:** if the gateway is set to `toggle-port` instead of `reboot`
(see step 2 below), only its WAN interface goes down — the LAN side, and
therefore this Pi's connectivity, is unaffected. That step of the run
actually waits for confirmation instead of firing and forgetting.

---

## Requirements

- Python 3.8+
- SSH access enabled on the gateway and on adopted switches/APs (see above)
- `systemd` (default on Raspberry Pi OS / most Debian-based distros) for the daily timer
- Optional: `tabulate` for prettier `list` output

---

## Installation

> **Debian / Ubuntu / Raspberry Pi OS**: system Python is protected (PEP 668).
> Use a virtual environment — the commands below handle that automatically.

```bash
git clone https://github.com/jakobneri/unifi-helper.git
cd unifi-helper

# create a virtual environment and install
python3 -m venv ~/.venv/unifi-restart
source ~/.venv/unifi-restart/bin/activate
pip install -e .

# optional: prettier tables
pip install tabulate
```

From inside the repo directory, run the tool via the included wrapper —
no venv activation needed:

```bash
./unifi-restart --help
```

(This is what the rest of this README uses. If you'd rather have
`unifi-restart` available everywhere, activate the venv with
`source ~/.venv/unifi-restart/bin/activate`, or symlink the wrapper into
`/usr/local/bin`.)

> The `install-timer` command (below) looks for the tool at
> `~/.venv/unifi-restart/bin/unifi-restart` first, so installing into that
> exact path avoids any extra configuration.

---

## Step-by-step setup

### 1. Enable SSH on your devices

- **Gateway** (UDM/UDM Pro/UDM SE/UniFi Express): Settings → System →
  Advanced (or Console access) → enable SSH, set/confirm the local root
  password.
- **Switches/APs**: Settings → System → SSH Authentication → enable "Device
  SSH Authentication" and set a shared username/password. UniFi pushes this
  to every adopted device.

### 2. Run the configuration wizard

```bash
./unifi-restart config
```

For each device you'll be asked for a name, host/IP, SSH port (default
`22`), SSH username, SSH password, and whether it's the gateway. The wizard
lets you add, edit, or remove devices in a loop — run it again any time to
change something.

**For the gateway only**, you get an extra choice: reboot it like every
other device, or just **toggle the WAN interface** (e.g. `eth1`) down and
back up for a configurable number of seconds (default 30s). This forces a
PPPoE/DHCP renegotiation — useful for ISPs like O2 that assign a new public
IP on reconnect — without rebooting the gateway or touching the LAN side at
all, so your other devices (and this Pi) stay online throughout.

Settings are saved to `~/.config/unifi-restart/config.json` (mode 600,
passwords stored in plain text — protected only by file permissions, same
as SSH keys typically are on a single-user Pi).

---

### 3. List configured devices

Check what's configured and in what order devices will be restarted (gateway always last):

```bash
./unifi-restart list
```

Example output:

```
Name            Host           Port  Username  Gateway  Action
--------------  -------------  ----  --------  -------  -----------------
Living Room AP  192.168.188.2  22    ubnt      no       reboot
Office Switch   192.168.188.3  22    ubnt      no       reboot
EX7 Gateway     192.168.188.1  22    root      yes      toggle eth1 (30s)
```

---

### 4. Do a dry run

```bash
./unifi-restart run --dry-run
```

---

### 5. Restart everything now

```bash
./unifi-restart run
```

---

### 6. Install the daily 3am timer

```bash
sudo ./unifi-restart install-timer
```

This installs and enables a `systemd` service + timer
(`/etc/systemd/system/unifi-restart.{service,timer}`) that runs
`unifi-restart run` every day at **03:00**, as the user who invoked `sudo`
(so it picks up that user's config).

The timer intentionally does **not** use systemd's `Persistent=true`
catch-up behavior: it only ever runs at the exact scheduled time, never
immediately on install/boot just because that time already passed today.
If you installed the timer before this was fixed, re-run `install-timer`
(same command as above) once to regenerate the unit file with the fix.

Choose a different time:

```bash
sudo ./unifi-restart install-timer --time 03:30
```

Remove the timer:

```bash
sudo ./unifi-restart install-timer --remove
```

---

### 7. Check status

```bash
./unifi-restart status
```

Shows the timer's next scheduled run plus the last few log entries:

```
NEXT                        LEFT     LAST                         PASSED  UNIT                 ACTIVATES
Mon 2026-07-07 03:00:00 UTC 8h left  Sun 2026-07-06 03:00:00 UTC   16h ago unifi-restart.timer  unifi-restart.service

Last log entries (~/.local/state/unifi-restart/restart.log):
  2026-07-06 03:00:01 INFO Starting full network restart (3 devices)
  2026-07-06 03:00:01 INFO Living Room AP: reboot command sent
  2026-07-06 03:00:02 INFO Office Switch: reboot command sent
  2026-07-06 03:00:32 INFO EX7 Gateway: WAN port eth1 toggled (30s down)
  2026-07-06 03:00:02 INFO Restart sequence complete (device reboots happen asynchronously)
```

---

## Command reference

```
./unifi-restart config                          # interactive device configuration (add/edit/remove)
./unifi-restart list                            # list configured devices and restart order
./unifi-restart run [--dry-run]                 # reboot every configured device now
sudo ./unifi-restart install-timer [--time HH:MM] [--remove]  # manage the daily systemd timer
./unifi-restart status                          # show timer status + recent log entries
```

---

## Config & state file locations

| File | Purpose |
|------|---------|
| `~/.config/unifi-restart/config.json` | Configured devices (host, SSH credentials, gateway flag) |
| `~/.local/state/unifi-restart/restart.log` | Log of every restart run |
| `/etc/systemd/system/unifi-restart.service` | Installed by `install-timer` |
| `/etc/systemd/system/unifi-restart.timer` | Installed by `install-timer` |
