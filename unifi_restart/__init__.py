"""unifi-restart: Restart an entire UniFi network (all switches, APs, gateway)
on a daily schedule via a systemd timer, driven from a device on that same
network (e.g. a Raspberry Pi).

Because the controlling device is itself normally connected through one of
the UniFi devices being restarted, it will briefly lose network connectivity
during the run. Restart commands are fired off without waiting for devices
to come back, and the gateway/UDM is restarted last so the run has the best
chance of reaching every device before local connectivity drops.
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

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_DIR = Path.home() / ".config" / "unifi-restart"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSION_FILE = CONFIG_DIR / "session.json"
STATE_DIR = Path.home() / ".local" / "state" / "unifi-restart"
LOG_FILE = STATE_DIR / "restart.log"

SERVICE_NAME = "unifi-restart.service"
TIMER_NAME = "unifi-restart.timer"
SYSTEMD_DIR = Path("/etc/systemd/system")

REQUEST_TIMEOUT = 10  # seconds; fail fast once the local link starts dropping


# ---------------------------------------------------------------------------
# Config / session persistence
# ---------------------------------------------------------------------------

def _normalize_host(host):
    host = host.strip().rstrip("/")
    if not re.match(r"^https?://", host):
        host = f"https://{host}"
    return host


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
        self.host = _normalize_host(config["host"])
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
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            body = e.response.text.strip() if e.response is not None else ""
            detail = f"\n{body}" if body else ""
            print(f"Login failed: {e}{detail}", file=sys.stderr)
            if e.response is not None and e.response.status_code == 403:
                print(
                    "\n403 usually means either the credentials are rejected before "
                    "auth even runs, or 'mode' doesn't match this controller "
                    "(standalone vs unifi-os). Run 'unifi-restart config' and double-"
                    "check: UDM/UDM Pro/UDM SE -> unifi-os; self-hosted Network "
                    "Server (e.g. on a Pi/Cloud Key) -> standalone.",
                    file=sys.stderr,
                )
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
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
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

    def restart_device(self, mac, hard=False):
        payload = {"cmd": "restart", "mac": mac}
        if hard:
            payload["reboot_type"] = "hard"
        self._request("POST", self._url("cmd/devmgr"), json=payload)


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
# Commands
# ---------------------------------------------------------------------------

def cmd_config(_args):
    existing = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            existing = json.load(f)

    print("UniFi Restart Configuration Setup")
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
    # Reset host default when mode changes to avoid carrying over the wrong port
    if mode != existing_mode:
        existing_host = default_host
    else:
        existing_host = existing.get("host", default_host)
    host = input(f"Host [{existing_host}]: ").strip()
    if not host:
        host = existing_host
    host = _normalize_host(host)

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
            d.get("type", "—"),
        ]
        for d in devices
    ]
    print(format_table(rows, ["Name", "MAC", "Model", "Type"]))


# Gateway/UDM devices are restarted last: they usually sit upstream of
# switches and APs, so restarting them first would cut the run short by
# taking the whole LAN's uplink down before other devices get their command.
_GATEWAY_TYPES = {"ugw", "udm"}


def _restart_order(devices):
    return sorted(devices, key=lambda d: 1 if d.get("type") in _GATEWAY_TYPES else 0)


def cmd_run(args):
    config = load_config()
    client = UnifiClient(config)
    devices = _fetch_devices(client)

    if not devices:
        print("No devices found.")
        return

    devices = _restart_order(devices)

    if args.dry_run:
        print("Would restart the following devices (in this order):")
        for d in devices:
            label = d.get("name") or d.get("hostname") or d.get("mac")
            print(f"  {label} ({d.get('type', '?')})")
        return

    logger = _get_logger()
    logger.info("Starting full network restart (%d devices, hard=%s)", len(devices), args.hard)

    exit_code = 0
    for d in devices:
        mac = d.get("mac")
        label = d.get("name") or d.get("hostname") or mac
        try:
            client.restart_device(mac, hard=args.hard)
            print(f"{label}: restart command sent")
            logger.info("%s: restart command sent", label)
        except (requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as e:
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
    hard_flag = " --hard" if args.hard else ""

    service = f"""[Unit]
Description=UniFi network restart (all devices)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={user}
Environment=HOME={home}
ExecStart={script} run{hard_flag}
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
    print(f"Service will execute: {script} run{hard_flag}")


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
        prog="unifi-restart",
        description="Restart an entire UniFi network (switches, APs, gateway) on a schedule.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("config", help="Interactive setup wizard")
    sub.add_parser("list", help="List all devices that would be restarted")

    p_run = sub.add_parser("run", help="Restart every device now")
    p_run.add_argument("--dry-run", action="store_true", help="Show restart order without sending commands")
    p_run.add_argument("--hard", action="store_true", help="Power-cycle PoE devices instead of a soft reboot")

    p_install = sub.add_parser("install-timer", help="Install/remove the daily systemd timer (requires sudo)")
    p_install.add_argument("--time", default="03:00", metavar="HH:MM", help="Daily restart time (default 03:00)")
    p_install.add_argument("--hard", action="store_true", help="Have the timer run a hard (power-cycle) restart")
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
