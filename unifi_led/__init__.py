"""unifi-led: Control LED overrides on UniFi devices via the Network API."""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_DIR = Path.home() / ".config" / "unifi-led"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSION_FILE = CONFIG_DIR / "session.json"
CRON_MARKER = "# unifi-led-managed"


# ---------------------------------------------------------------------------
# Config / session persistence
# ---------------------------------------------------------------------------

def load_config():
    if not CONFIG_FILE.exists():
        print("No config found. Run 'unifi-led config' first.", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


def load_session():
    if SESSION_FILE.exists():
        with open(SESSION_FILE) as f:
            return json.load(f)
    return {}


def save_session(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(SESSION_FILE, 0o600)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class UnifiClient:
    def __init__(self, config):
        self.host = config["host"].rstrip("/")
        self.username = config["username"]
        self.password = config["password"]
        self.site = config.get("site", "default")
        self.mode = config.get("mode", "unifi-os")  # "unifi-os" or "standalone"
        self.session = requests.Session()
        self.session.verify = False
        self.csrf_token = None

        saved = load_session()
        if saved.get("cookies"):
            self.session.cookies.update(saved["cookies"])
        self.csrf_token = saved.get("csrf_token")

    def _url(self, path):
        if self.mode == "standalone":
            return f"{self.host}/api/s/{self.site}/{path}"
        return f"{self.host}/proxy/network/api/s/{self.site}/{path}"

    def login(self):
        if self.mode == "standalone":
            url = f"{self.host}/api/login"
        else:
            url = f"{self.host}/api/auth/login"
        try:
            resp = self.session.post(
                url,
                json={"username": self.username, "password": self.password},
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"Login failed: {e}", file=sys.stderr)
            sys.exit(1)
        # CSRF token only exists on UniFi OS
        if self.mode == "unifi-os":
            self.csrf_token = resp.headers.get("X-Csrf-Token")
        save_session({
            "cookies": dict(self.session.cookies),
            "csrf_token": self.csrf_token,
        })

    def _request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", {})
        if self.csrf_token:
            headers["X-Csrf-Token"] = self.csrf_token

        resp = self.session.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401:
            self.login()
            if self.csrf_token:
                headers["X-Csrf-Token"] = self.csrf_token
            resp = self.session.request(method, url, headers=headers, **kwargs)

        resp.raise_for_status()
        return resp

    def get_devices(self):
        resp = self._request("GET", self._url("stat/device"))
        return resp.json().get("data", [])

    def set_led(self, device_id, state):
        self._request("PUT", self._url(f"rest/device/{device_id}"), json={"led_override": state})


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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_config(_args):
    existing = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            existing = json.load(f)

    print("UniFi LED Configuration Setup")
    print("=" * 34)

    print("Mode:")
    print("  1) standalone  — UniFi Network Server on PC/Raspberry Pi (port 8443)")
    print("  2) unifi-os    — UDM / UDM Pro / UDM SE (port 443)")
    existing_mode = existing.get("mode", "unifi-os")
    mode_default = "1" if existing_mode == "standalone" else "2"
    mode_input = input(f"Choose [1/2, default {mode_default}]: ").strip()
    if mode_input == "1":
        mode = "standalone"
    elif mode_input == "2":
        mode = "unifi-os"
    else:
        mode = existing_mode

    default_host = "https://localhost:8443" if mode == "standalone" else "https://localhost"
    existing_host = existing.get("host", default_host)
    host = input(f"Host [{existing_host}]: ").strip()
    if not host:
        host = existing_host

    username = input(f"Username [{existing.get('username', 'admin')}]: ").strip()
    if not username:
        username = existing.get("username", "admin")

    password = getpass.getpass("Password (blank keeps existing): ").strip()
    if not password:
        password = existing.get("password", "")

    site = input(f"Site [{existing.get('site', 'default')}]: ").strip()
    if not site:
        site = existing.get("site", "default")

    save_config({"host": host, "username": username, "password": password, "site": site, "mode": mode})
    print(f"\nConfig saved to {CONFIG_FILE} (mode: {mode})")


def cmd_list(_args):
    config = load_config()
    client = UnifiClient(config)
    devices = _fetch_devices(client)

    if not devices:
        print("No devices found.")
        return

    rows = [
        [
            d.get("name") or d.get("hostname") or "—",
            d.get("mac", "—"),
            d.get("model", "—"),
            d.get("led_override", "default"),
        ]
        for d in devices
    ]
    print(format_table(rows, ["Name", "MAC", "Model", "LED Override"]))


def cmd_set(args):
    state = args.state
    config = load_config()
    client = UnifiClient(config)
    devices = _fetch_devices(client)

    if args.target == "all":
        targets = devices
    else:
        mac_lower = args.target.lower()
        targets = [d for d in devices if d.get("mac", "").lower() == mac_lower]
        if not targets:
            print(f"No device found with MAC {args.target}", file=sys.stderr)
            sys.exit(1)

    exit_code = 0
    for device in targets:
        device_id = device["_id"]
        label = device.get("name") or device.get("mac") or device_id
        try:
            client.set_led(device_id, state)
            print(f"{label}: led_override → {state}")
        except (requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as e:
            print(f"{label}: error — {e}", file=sys.stderr)
            exit_code = 1

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Cron helpers
# ---------------------------------------------------------------------------

def _get_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _set_crontab(lines):
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    proc = subprocess.run(["crontab", "-"], input=content, text=True)
    if proc.returncode != 0:
        print("Failed to update crontab.", file=sys.stderr)
        sys.exit(1)


def _strip_managed(lines):
    out = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == CRON_MARKER:
            i += 2  # skip marker + entry
        else:
            out.append(lines[i])
            i += 1
    return out


def _resolve_script():
    import shutil
    path = shutil.which("unifi-led")
    if path:
        return path
    return f"{sys.executable} -m unifi_led"


def _parse_time(t):
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if not m:
        print(f"Invalid time '{t}'. Use HH:MM.", file=sys.stderr)
        sys.exit(1)
    return int(m.group(1)), int(m.group(2))


def cmd_schedule(args):
    if args.remove:
        lines = _get_crontab()
        _set_crontab(_strip_managed(lines))
        print("Removed managed crontab entries.")
        return

    if not args.on or not args.off:
        print("Both --on and --off are required (or use --remove).", file=sys.stderr)
        sys.exit(1)

    on_h, on_m = _parse_time(args.on)
    off_h, off_m = _parse_time(args.off)
    script = _resolve_script()

    lines = _strip_managed(_get_crontab())
    lines += [
        CRON_MARKER,
        f"{on_m} {on_h} * * * {script} set all on",
        CRON_MARKER,
        f"{off_m} {off_h} * * * {script} set all off",
    ]
    _set_crontab(lines)
    print(f"Scheduled: on at {on_h:02d}:{on_m:02d}, off at {off_h:02d}:{off_m:02d}")


def cmd_status(_args):
    lines = _get_crontab()
    managed = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == CRON_MARKER and i + 1 < len(lines):
            managed.append(lines[i + 1])
            i += 2
        else:
            i += 1

    if managed:
        print("Managed schedule:")
        for entry in managed:
            parts = entry.split()
            if len(parts) >= 6:
                minute, hour = parts[0], parts[1]
                action = " ".join(parts[5:])
                print(f"  {int(hour):02d}:{minute.zfill(2)}  {action}")
            else:
                print(f"  {entry}")
    else:
        print("No managed schedule.")

    print()

    config = load_config()
    client = UnifiClient(config)
    devices = _fetch_devices(client)

    if not devices:
        print("No devices found.")
        return

    rows = [
        [
            d.get("name") or d.get("hostname") or "—",
            d.get("mac", "—"),
            d.get("led_override", "default"),
        ]
        for d in devices
    ]
    print(format_table(rows, ["Name", "MAC", "LED Override"]))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fetch_devices(client):
    try:
        return client.get_devices()
    except requests.exceptions.ConnectionError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"API error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="unifi-led",
        description="Control LED overrides on UniFi devices.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("config", help="Interactive setup wizard")
    sub.add_parser("list", help="List all devices and their LED state")

    p_set = sub.add_parser("set", help="Set LED override for a device or all devices")
    p_set.add_argument("target", metavar="mac|all", help="Device MAC address or 'all'")
    p_set.add_argument("state", choices=["on", "off", "default"])

    p_sched = sub.add_parser("schedule", help="Manage LED on/off schedule via crontab")
    p_sched.add_argument("--on", metavar="HH:MM", help="Time to turn LEDs on")
    p_sched.add_argument("--off", metavar="HH:MM", help="Time to turn LEDs off")
    p_sched.add_argument("--remove", action="store_true", help="Remove managed entries")

    sub.add_parser("status", help="Show schedule and current LED state of all devices")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "config": cmd_config,
        "list": cmd_list,
        "set": cmd_set,
        "schedule": cmd_schedule,
        "status": cmd_status,
    }
    dispatch[args.command](args)
