"""unifi-restart: Restart an entire UniFi network (all switches, APs, gateway)
on a daily schedule via a systemd timer, driven from a device on that same
network (e.g. a Raspberry Pi).

Devices are rebooted over plain SSH using their local console credentials
(the gateway's root account, and the shared "Device SSH Authentication"
credentials UniFi pushes to adopted switches/APs) rather than the UniFi
Network API. That sidesteps the API's session/SSO login entirely, so
there's nothing to lock you out of.

Because the controlling device is itself normally connected through one of
the UniFi devices being restarted, it will briefly lose network connectivity
during the run. Reboots are fired off without waiting for devices to come
back, and the gateway is restarted last so the run has the best chance of
reaching every device before local connectivity drops.
"""

import argparse
import getpass
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import paramiko

CONFIG_DIR = Path.home() / ".config" / "unifi-restart"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_DIR = Path.home() / ".local" / "state" / "unifi-restart"
LOG_FILE = STATE_DIR / "restart.log"

SERVICE_NAME = "unifi-restart.service"
TIMER_NAME = "unifi-restart.timer"
SYSTEMD_DIR = Path("/etc/systemd/system")

SSH_TIMEOUT = 8  # seconds; fail fast once the local link starts dropping
SSH_PORT_DEFAULT = 22
TOGGLE_SECONDS_DEFAULT = 30


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def _load_config_or_empty():
    if not CONFIG_FILE.exists():
        return {"devices": []}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_config():
    if not CONFIG_FILE.exists():
        print("No config found. Run 'unifi-restart config' first.", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


# ---------------------------------------------------------------------------
# SSH reboot
# ---------------------------------------------------------------------------

def _ssh_reboot(device):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            device["host"],
            port=device.get("port", SSH_PORT_DEFAULT),
            username=device["username"],
            password=device["password"],
            timeout=SSH_TIMEOUT,
            banner_timeout=SSH_TIMEOUT,
            auth_timeout=SSH_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        client.exec_command("reboot", timeout=SSH_TIMEOUT)
    finally:
        client.close()


def _ssh_toggle_port(device, interface, hold_seconds):
    # Only the WAN link goes down here, not the LAN side we're SSHing in
    # over, so — unlike a reboot — this command actually completes and we
    # can wait for its real exit status.
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            device["host"],
            port=device.get("port", SSH_PORT_DEFAULT),
            username=device["username"],
            password=device["password"],
            timeout=SSH_TIMEOUT,
            banner_timeout=SSH_TIMEOUT,
            auth_timeout=SSH_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        cmd = f"ip link set {interface} down && sleep {hold_seconds} && ip link set {interface} up"
        _, stdout, stderr = client.exec_command(cmd, timeout=hold_seconds + SSH_TIMEOUT)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            detail = stderr.read().decode(errors="replace").strip()
            raise RuntimeError(detail or f"'{cmd}' exited with status {exit_status}")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Table formatting
# ---------------------------------------------------------------------------

def format_table(rows, headers):
    try:
        from tabulate import tabulate
        return tabulate(rows, headers=headers, tablefmt="simple")
    except ImportError:
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(cell)))
        sep = "  "
        fmt = sep.join(f"{{:<{w}}}" for w in col_widths)
        lines = [
            fmt.format(*headers),
            sep.join("-" * w for w in col_widths),
        ]
        for row in rows:
            lines.append(fmt.format(*[str(c) for c in row]))
        return "\n".join(lines)


def _action_label(d):
    if d.get("action") == "toggle-port":
        return f"toggle {d.get('interface', '?')} ({d.get('toggle_seconds', TOGGLE_SECONDS_DEFAULT)}s)"
    return "reboot"


def _print_devices(devices):
    rows = [
        [
            d.get("name") or "—",
            d["host"],
            d.get("port", SSH_PORT_DEFAULT),
            d.get("username", "root"),
            "yes" if d.get("gateway") else "no",
            _action_label(d),
        ]
        for d in devices
    ]
    print(format_table(rows, ["Name", "Host", "Port", "Username", "Gateway", "Action"]))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _get_logger():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("unifi_restart")
    if not logger.handlers:
        handler = logging.FileHandler(LOG_FILE)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# ---------------------------------------------------------------------------
# Config wizard
# ---------------------------------------------------------------------------

def _prompt_device(existing=None):
    existing = existing or {}

    name = input(f"Name [{existing.get('name', '')}]: ").strip() or existing.get("name", "")
    host = input(f"Host/IP [{existing.get('host', '')}]: ").strip() or existing.get("host", "")

    port_default = existing.get("port", SSH_PORT_DEFAULT)
    port_in = input(f"SSH port [{port_default}]: ").strip()
    port = int(port_in) if port_in else port_default

    user_default = existing.get("username", "root")
    username = input(f"SSH username [{user_default}]: ").strip() or user_default

    password = getpass.getpass("SSH password (blank keeps existing): ").strip()
    if not password:
        password = existing.get("password", "")

    gw_default = "y" if existing.get("gateway") else "n"
    gw_in = input(f"Is this the gateway/router? [y/N, default {gw_default}]: ").strip().lower()
    gateway = (gw_in == "y") if gw_in else existing.get("gateway", False)

    action = existing.get("action", "reboot")
    interface = existing.get("interface", "")
    toggle_seconds = existing.get("toggle_seconds", TOGGLE_SECONDS_DEFAULT)

    if gateway:
        action_default = "t" if action == "toggle-port" else "r"
        action_in = input(
            "On restart, [r]eboot the whole gateway or just [t]oggle the WAN port "
            f"to renew the public IP (e.g. PPPoE with dynamic IP)? [R/t, default {action_default}]: "
        ).strip().lower()
        if action_in:
            action = "toggle-port" if action_in == "t" else "reboot"

        if action == "toggle-port":
            iface_default = interface or "eth1"
            interface = input(f"WAN interface to toggle (e.g. Port 2 is usually eth1) [{iface_default}]: ").strip() or iface_default
            seconds_in = input(f"Seconds to hold the port down [{toggle_seconds}]: ").strip()
            toggle_seconds = int(seconds_in) if seconds_in else toggle_seconds
    else:
        action = "reboot"

    device = {
        "name": name,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "gateway": gateway,
        "action": action,
    }
    if action == "toggle-port":
        device["interface"] = interface
        device["toggle_seconds"] = toggle_seconds
    return device


def _prompt_index(devices, verb):
    if not devices:
        print("No devices configured.")
        return None
    for i, d in enumerate(devices):
        print(f"  {i + 1}) {d.get('name') or d['host']} ({d['host']})")
    raw = input(f"Which device to {verb}? [1-{len(devices)}]: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(devices)):
        print("Invalid selection.")
        return None
    return int(raw) - 1


def cmd_config(_args):
    config = _load_config_or_empty()
    devices = config.get("devices", [])

    print("UniFi Restart – Device Configuration (SSH-based)")
    print("=" * 48)
    print(
        "\nEach device needs SSH access via its local console credentials:\n"
        "  - Gateway (e.g. EX7/UDM): Settings > System > Advanced/Console -> enable SSH, root account\n"
        "  - Switches/APs: Settings > System > SSH Authentication -> set a shared SSH username/password\n"
    )
    print("Current devices:" if devices else "No devices configured yet.")
    if devices:
        _print_devices(devices)

    while True:
        print("\n[a]dd  [e]dit  [r]emove  [d]one")
        choice = input("Choice: ").strip().lower()
        if choice == "a":
            devices.append(_prompt_device())
        elif choice == "e":
            idx = _prompt_index(devices, "edit")
            if idx is not None:
                devices[idx] = _prompt_device(existing=devices[idx])
        elif choice == "r":
            idx = _prompt_index(devices, "remove")
            if idx is not None:
                removed = devices.pop(idx)
                print(f"Removed {removed.get('name') or removed['host']}")
        elif choice == "d":
            break
        else:
            print("Unknown choice.")
            continue
        print("\nCurrent devices:")
        _print_devices(devices) if devices else print("  (none)")

    save_config({"devices": devices})
    print(f"\nSaved {len(devices)} device(s) to {CONFIG_FILE}")


def cmd_list(_args):
    config = load_config()
    devices = config.get("devices", [])
    if not devices:
        print("No devices configured. Run 'unifi-restart config' first.")
        return
    _print_devices(_restart_order(devices))


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def _restart_order(devices):
    # Gateways are restarted last: they usually sit upstream of switches and
    # APs, so restarting one first would cut the run short by taking the
    # whole LAN's uplink down before the remaining devices get their command.
    return sorted(devices, key=lambda d: 1 if d.get("gateway") else 0)


def cmd_run(args):
    config = load_config()
    devices = config.get("devices", [])
    if not devices:
        print("No devices configured. Run 'unifi-restart config' first.")
        return

    devices = _restart_order(devices)

    if args.dry_run:
        print("Would perform the following actions (in this order):")
        for d in devices:
            label = d.get("name") or d["host"]
            print(f"  {label} ({d['host']}) -> {_action_label(d)}{' [gateway]' if d.get('gateway') else ''}")
        return

    logger = _get_logger()
    logger.info("Starting full network restart (%d devices)", len(devices))

    exit_code = 0
    for d in devices:
        label = d.get("name") or d["host"]
        try:
            if d.get("action") == "toggle-port":
                interface = d["interface"]
                seconds = d.get("toggle_seconds", TOGGLE_SECONDS_DEFAULT)
                _ssh_toggle_port(d, interface, seconds)
                msg = f"{label}: WAN port {interface} toggled ({seconds}s down)"
            else:
                _ssh_reboot(d)
                msg = f"{label}: reboot command sent"
            print(msg)
            logger.info(msg)
        except Exception as e:
            # Expected once the device carrying our own uplink goes down.
            print(f"{label}: error — {e}", file=sys.stderr)
            logger.warning("%s: error — %s", label, e)
            exit_code = 1

    logger.info("Restart sequence complete (device reboots happen asynchronously)")
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# systemd timer management
# ---------------------------------------------------------------------------

def _parse_time(t):
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if not m:
        print(f"Invalid time '{t}'. Use HH:MM.", file=sys.stderr)
        sys.exit(1)
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        print(f"Invalid time '{t}'. Use HH:MM.", file=sys.stderr)
        sys.exit(1)
    return hour, minute


def _target_user():
    return os.environ.get("SUDO_USER") or getpass.getuser()


def _target_home(user):
    import pwd
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return Path.home()


def _resolve_script(user, home):
    venv_bin = home / ".venv" / "unifi-restart" / "bin" / "unifi-restart"
    if venv_bin.exists():
        return str(venv_bin)
    import shutil
    path = shutil.which("unifi-restart")
    if path:
        return path
    return f"{sys.executable} -m unifi_restart"


def cmd_install_timer(args):
    if os.geteuid() != 0:
        print("Run this with sudo — systemd unit files live under /etc/systemd/system.", file=sys.stderr)
        sys.exit(1)

    if args.remove:
        subprocess.run(["systemctl", "disable", "--now", TIMER_NAME], check=False)
        (SYSTEMD_DIR / SERVICE_NAME).unlink(missing_ok=True)
        (SYSTEMD_DIR / TIMER_NAME).unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        print("Removed the unifi-restart timer.")
        return

    hour, minute = _parse_time(args.time)
    user = _target_user()
    home = _target_home(user)
    script = _resolve_script(user, home)

    service = f"""[Unit]
Description=UniFi network restart (all devices)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={user}
Environment=HOME={home}
ExecStart={script} run
"""

    timer = f"""[Unit]
Description=Daily UniFi network restart at {hour:02d}:{minute:02d}

[Timer]
OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
Persistent=true

[Install]
WantedBy=timers.target
"""

    (SYSTEMD_DIR / SERVICE_NAME).write_text(service)
    (SYSTEMD_DIR / TIMER_NAME).write_text(timer)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", TIMER_NAME], check=True)
    print(f"Installed {TIMER_NAME}: daily restart at {hour:02d}:{minute:02d}, running as user '{user}'.")
    print(f"Service will execute: {script} run")


def cmd_status(_args):
    result = subprocess.run(
        ["systemctl", "list-timers", TIMER_NAME, "--all", "--no-pager"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and TIMER_NAME in result.stdout:
        print(result.stdout.strip())
    else:
        print(f"{TIMER_NAME} is not installed. Run 'sudo unifi-restart install-timer' to set it up.")

    print()
    if LOG_FILE.exists():
        print(f"Last log entries ({LOG_FILE}):")
        for line in LOG_FILE.read_text().splitlines()[-10:]:
            print(f"  {line}")
    else:
        print("No restart log yet.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="unifi-restart",
        description="Restart an entire UniFi network (switches, APs, gateway) over SSH, on a schedule.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("config", help="Interactive device configuration (add/edit/remove)")
    sub.add_parser("list", help="List configured devices and restart order")

    p_run = sub.add_parser("run", help="Reboot every configured device now")
    p_run.add_argument("--dry-run", action="store_true", help="Show restart order without sending commands")

    p_install = sub.add_parser("install-timer", help="Install/remove the daily systemd timer (requires sudo)")
    p_install.add_argument("--time", default="03:00", metavar="HH:MM", help="Daily restart time (default 03:00)")
    p_install.add_argument("--remove", action="store_true", help="Remove the installed timer")

    sub.add_parser("status", help="Show timer status and recent restart log entries")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "config": cmd_config,
        "list": cmd_list,
        "run": cmd_run,
        "install-timer": cmd_install_timer,
        "status": cmd_status,
    }
    dispatch[args.command](args)
