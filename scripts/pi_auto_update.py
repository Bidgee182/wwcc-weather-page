#!/usr/bin/env python3
"""
Auto-update watcher for pi_pump_poller.py.

Polls GitHub every 5 minutes for the latest commit SHA of pi_pump_poller.py.
When it changes, downloads the new version and restarts pump-poller.service.

Runs as a systemd service (pump-updater.service). Requires passwordless sudo for
systemctl restart - add this line to /etc/sudoers.d/pump-updater:
    andrew ALL=(ALL) NOPASSWD: /bin/systemctl restart pump-poller

Setup on Pi:
    cp /home/andrew/pump_poller/pi_auto_update.py /home/andrew/pump_poller/
    sudo cp /home/andrew/pump_poller/pump-updater.service /etc/systemd/system/
    echo 'andrew ALL=(ALL) NOPASSWD: /bin/systemctl restart pump-poller' | sudo tee /etc/sudoers.d/pump-updater
    sudo chmod 440 /etc/sudoers.d/pump-updater
    sudo systemctl enable --now pump-updater
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

GITHUB_REPO  = "Bidgee182/wwcc-weather-page"
GITHUB_FILE  = "scripts/pi_pump_poller.py"
LOCAL_FILE   = "/home/andrew/pump_poller/pi_pump_poller.py"
SHA_FILE     = "/home/andrew/pump_poller/.last_poller_sha"
SERVICE_NAME = "pump-poller"
POLL_INTERVAL = 300  # 5 minutes


def get_remote_sha():
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}/commits"
        f"?path={GITHUB_FILE}&per_page=1"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "pi-auto-update/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            commits = json.load(resp)
            if commits:
                return commits[0]["sha"]
    except Exception as e:
        print(f"[updater] ERROR fetching SHA: {e}", flush=True)
    return None


def get_local_sha():
    if os.path.exists(SHA_FILE):
        with open(SHA_FILE) as f:
            return f.read().strip()
    return None


def save_sha(sha):
    with open(SHA_FILE, "w") as f:
        f.write(sha)


def download_new_version():
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE}"
    req = urllib.request.Request(url, headers={"User-Agent": "pi-auto-update/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
        tmp = LOCAL_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, LOCAL_FILE)
        return True
    except Exception as e:
        print(f"[updater] ERROR downloading: {e}", flush=True)
        return False


def restart_service():
    result = subprocess.run(
        ["sudo", "/bin/systemctl", "restart", SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"[updater] {SERVICE_NAME} restarted OK", flush=True)
        return True
    print(f"[updater] restart failed: {result.stderr.strip()}", flush=True)
    return False


def main():
    print(f"[updater] Started. Polling GitHub every {POLL_INTERVAL}s.", flush=True)
    while True:
        remote_sha = get_remote_sha()
        if remote_sha:
            local_sha = get_local_sha()
            if local_sha != remote_sha:
                print(
                    f"[updater] New version: {remote_sha[:8]}"
                    f" (was {(local_sha or 'none')[:8]})",
                    flush=True,
                )
                if download_new_version():
                    print("[updater] Download OK - restarting service ...", flush=True)
                    if restart_service():
                        save_sha(remote_sha)
                    else:
                        print("[updater] Restart failed - will retry next cycle", flush=True)
                else:
                    print("[updater] Download failed - will retry next cycle", flush=True)
            else:
                print(f"[updater] Up to date ({remote_sha[:8]})", flush=True)
        else:
            print("[updater] Could not fetch remote SHA - will retry", flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
