#!/usr/bin/env python3
"""Automated WaterNSW DAS export -> water_usage.json import.

Runs on andrew-new (the standby poller PC). It drives a real browser because the
DAS portal builds the CSV in-browser over a WebSocket and the Download link is a
JavaScript blob (href="#") - there is no plain URL/API to fetch. A dedicated
browser profile holds the login session, so NO password is stored anywhere: you
log in ONCE by hand (--login), and every daily run reuses that session. If the
session ever expires it emails you to log in again instead of failing silently.

The export is "Delimited Text / 7 Days / all 9 params", so every run re-covers the
last week; water_usage_build.py --import-csv max-merges it (a partial re-read never
lowers a good day), exactly like the admin "Upload DAS CSV" button.

Setup on andrew-new (one time):
    pip install playwright
    playwright install chromium
    python scripts/das_auto_export.py --login      # headed: sign in, then close the window

Daily (Task Scheduler, headless):
    python scripts/das_auto_export.py

Watch it run without committing (to tune/confirm the clicks):
    python scripts/das_auto_export.py --test

If the Export/Download clicks ever stop matching the portal, re-record them with:
    playwright codegen https://das.waternsw.com.au/ui/nswd510/40CA413688-120922-3/120922-3-LID01/?m=show&v=map
and update open_export_dialog() / export_and_download() below.
"""
import argparse
import datetime
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Browser profile lives OUTSIDE the repo so the login session is never committed.
PROFILE_DIR = ROOT.parent / "das_profile"
PORTAL = "https://das.waternsw.com.au/ui/nswd510/40CA413688-120922-3/"
# Location (meter) view - the page the export dialog opens from.
LOCATION_URL = PORTAL + "120922-3-LID01/?m=show&v=map"
ALERT_TO = ["andrew@bidgeepumps.com.au"]


def log(*a):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def alert_login_needed(reason):
    """Best-effort email asking Andrew to re-seed the DAS login session."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import mailer
        html = (
            "<p>The automated WaterNSW DAS export could not run because the saved "
            "login session is no longer valid.</p>"
            f"<p><b>Reason:</b> {reason}</p>"
            "<p>On andrew-new, run:<br><code>python scripts/das_auto_export.py --login</code>"
            "<br>sign in, close the window, and the daily task will resume.</p>"
        )
        ok, detail = mailer.send_html(
            "DAS auto-export: please re-login", html, ALERT_TO, stream="default")
        log("login-needed email:", "sent" if ok else detail)
    except Exception as e:  # never let the alert crash the run
        log("could not send login-needed email:", e)


def is_logged_in(page):
    """Logged in when the DAS app chrome is present, not the WaterNSW login page."""
    url = (page.url or "").lower()
    if "login" in url or "logon" in url or "auth" in url:
        return False
    # The app shell shows the CREATE button / workspace tree once authenticated.
    try:
        page.wait_for_selector("text=CREATE", timeout=15000)
        return True
    except Exception:
        return False


def open_export_dialog(page):
    """Open Historic data and switch to its Export tab.

    The Export tab is a stable anchor (href="#historic_export"); getting there means
    opening the "Historic data" dialog first. The dialog is a modal opened from the
    location view - the exact trigger is confirmed on andrew-new via `playwright
    codegen` if it ever changes. We try the known paths and fall back with guidance.
    """
    page.goto(LOCATION_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)  # the SPA loads its chart over a WebSocket

    export_tab = 'a[href="#historic_export"]'
    if page.locator(export_tab).count():
        page.click(export_tab)
        return

    # Open the "Historic data" dialog, then the Export tab.
    for trigger in [
        page.get_by_role("link", name="Historic data"),
        page.get_by_role("button", name="Historic data"),
        page.get_by_text("Historic data", exact=True),
    ]:
        try:
            if trigger.count():
                trigger.first.click()
                page.wait_for_selector(export_tab, timeout=8000)
                page.click(export_tab)
                return
        except Exception:
            continue

    raise RuntimeError(
        "Could not find the Historic data / Export control. Re-record with "
        "`playwright codegen " + LOCATION_URL + "` and update open_export_dialog().")


def export_and_download(page, dest):
    """On the Export tab: run the export job and save the resulting CSV to `dest`."""
    # Defaults are already Delimited Text / 7 Days / all 9 params. Be explicit on format.
    try:
        page.select_option("select", value="DELIMITED_TEXT")
    except Exception:
        pass

    page.get_by_role("button", name="Export", exact=True).click()
    # Wait for the job to finish (the Download link becomes active).
    page.wait_for_selector("text=Successfully completed", timeout=120000)

    with page.expect_download(timeout=120000) as dl:
        page.get_by_role("link", name="Download", exact=True).click()
    dl.value.save_as(str(dest))
    log("downloaded", dest, f"({dest.stat().st_size} bytes)")


def import_csv(dest):
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "water_usage_build.py"),
         "--import-csv", str(dest)],
        cwd=ROOT, check=True)


def commit_and_push():
    files = ["data/water_usage.json", "data/water_register_daily.json",
             "data/water_register_hourly.json"]
    subprocess.run(["git", "add", *files], cwd=ROOT, check=True)
    changed = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT).returncode != 0
    if not changed:
        log("no water-usage changes to commit")
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run(["git", "commit", "-m", f"DAS auto-export import {stamp}"], cwd=ROOT, check=True)
    for attempt in range(1, 6):
        pull = subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"], cwd=ROOT)
        push = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT)
        if pull.returncode == 0 and push.returncode == 0:
            log("pushed")
            return
        log(f"push attempt {attempt} failed, retrying")
    log("WARNING: could not push after 5 attempts")


def run(mode):
    from playwright.sync_api import sync_playwright
    headed = mode in ("login", "test")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=not headed, accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if mode == "login":
                page.goto(PORTAL, wait_until="domcontentloaded")
                log("Sign in in the window that opened, then close it. The session "
                    "is saved to", PROFILE_DIR)
                # Keep the browser open until Andrew closes it.
                page.wait_for_event("close", timeout=0)
                return 0

            page.goto(PORTAL, wait_until="domcontentloaded")
            if not is_logged_in(page):
                alert_login_needed("session expired / not signed in")
                log("NOT logged in - emailed Andrew, stopping")
                return 2

            dest = ROOT / f"export-nswd510-{datetime.date.today():%Y-%m-%d}.csv"
            open_export_dialog(page)
            export_and_download(page, dest)
            import_csv(dest)
            if mode == "run":
                commit_and_push()
            else:
                log("--test: skipping git commit/push")
            return 0
        finally:
            if mode != "login":
                ctx.close()


def main():
    ap = argparse.ArgumentParser(description="Automated WaterNSW DAS export import")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--login", action="store_true", help="headed: seed the login session once")
    g.add_argument("--test", action="store_true", help="headed full run, no git commit")
    args = ap.parse_args()
    mode = "login" if args.login else "test" if args.test else "run"
    try:
        sys.exit(run(mode))
    except Exception as e:
        log("ERROR:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
