#!/usr/bin/env python3
"""Automated WaterNSW DAS export -> water_usage.json import.

Runs on andrew-new (the standby poller PC). It drives a real browser because the
DAS portal builds the export in-browser and hands it over as a download blob - there
is no plain URL/API to fetch. A dedicated browser profile holds the login session, so
NO password is stored anywhere: you log in ONCE by hand (--login), and every daily run
reuses that session. If the session ever expires it emails you to log in again instead
of failing silently.

THE EXPORT (verified 17 Nov 2026): right-click the workspace node -> Historic data ->
Export downloads a ZIP of the LAST 7 DAYS (all 9 params, delimited text) DIRECTLY - no
dialog, no format/range picker. The context-menu "Export" <a> carries an onclick
handler that fires even though its submenu is CSS-hidden, so we just dispatch a native
click on it and capture the download. The ZIP holds one CSV in the exact format the
admin "Upload DAS CSV" button takes; water_usage_build.py --import-csv max-merges it (a
re-read of an already-loaded day is ignored / the more complete reading is kept), so
re-covering the last week every day is self-cleaning.

Setup on andrew-new (one time):
    pip install playwright tzdata      # tzdata: Windows has no system tz db, and the
                                       # tank step needs Australia/Sydney
    playwright install chromium
    python scripts/das_auto_export.py --login      # headed: sign in, then close the window

Daily (Task Scheduler, headless):
    python scripts/das_auto_export.py

Watch it run without committing (to tune/confirm the clicks):
    python scripts/das_auto_export.py --test

If the Export click ever stops matching the portal, re-record it with:
    playwright codegen "https://das.waternsw.com.au/ui/nswd510/40CA413688-120922-3/?m=show"
and update export_download() below. (The flow: right-click the 40CA413688-120922-3
workspace node -> Historic data -> Export.)
"""
import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Browser profile lives OUTSIDE the repo so the login session is never committed.
PROFILE_DIR = ROOT.parent / "das_profile"
PORTAL = "https://das.waternsw.com.au/ui/nswd510/40CA413688-120922-3/?m=show"
# The workspace tree node. Right-click it -> Historic data -> Export (a 7-day ZIP).
WORKSPACE = "40CA413688-120922-3"
ALERT_TO = ["andrew@bidgeepumps.com.au"]
# Heartbeat das_watchdog.py reads; committed with the data so a FAILED push leaves the
# remote heartbeat stale, which is exactly what the 48h watchdog should catch.
STATUS_FILE = ROOT / "data" / "das_auto_export_status.json"


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


def export_download(page, zip_dest):
    """Right-click the workspace node and fire Historic data -> Export, capturing the
    7-day ZIP it downloads straight away (no dialog).

    The context-menu items render into the DOM when the menu opens. The "Export" link
    (an <a> with an icon-export icon) has an onclick handler that fires even while its
    Bootstrap dropdown-submenu is CSS-hidden, so we dispatch a native click on it
    rather than fighting the hover-only flyout. Playwright captures the resulting
    download blob.
    """
    page.goto(PORTAL, wait_until="domcontentloaded")

    node = page.get_by_text(WORKSPACE, exact=True).filter(visible=True).first
    node.wait_for(timeout=30000)
    page.wait_for_timeout(800)
    node.click(button="right")  # open the context menu so its items enter the DOM

    # The Export <a> is present but hidden (submenu is hover-revealed). Wait for it to
    # attach, then fire its click handler directly and catch the download.
    page.wait_for_selector("a:has(i.icon-export)", state="attached", timeout=15000)
    with page.expect_download(timeout=120000) as dl:
        page.eval_on_selector("a:has(i.icon-export)", "el => el.click()")
    dl.value.save_as(str(zip_dest))
    log("downloaded", zip_dest.name, f"({zip_dest.stat().st_size} bytes)")


def import_export_zip(zip_path):
    """Pull the CSV out of the export ZIP and feed it to the overlap-safe importer."""
    with zipfile.ZipFile(zip_path) as z:
        csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csvs:
            raise RuntimeError("no CSV inside export zip: " + ", ".join(z.namelist()))
        out_csv = zip_path.parent / "das-export.csv"
        with z.open(csvs[0]) as src, open(out_csv, "wb") as dst:
            dst.write(src.read())
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "water_usage_build.py"),
         "--import-csv", str(out_csv)],
        cwd=ROOT, check=True)


def sync_repo():
    """Best-effort pull so the register max-merge and the tank rebuild both start from
    the latest committed data (the poller PC may have pushed newer readings)."""
    r = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=ROOT)
    if r.returncode != 0:
        log("WARNING: git pull before build failed; building from local files")


def build_tank_daily():
    """Record yesterday's tank water onto the course (sum of dips) -> water_tank_daily.json.

    Non-fatal on purpose: the DAS import has already succeeded by this point, so a
    tank-build hiccup (e.g. missing tzdata on Windows) must not abort the run and lose
    that import."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "tank_daily_build.py")], cwd=ROOT)
    if r.returncode != 0:
        log("WARNING: tank_daily_build failed (continuing with the lake import). "
            "On Windows this is usually the tz database: run  pip install tzdata")


def write_status():
    """Write the success heartbeat (last_success + a couple of figures for context)."""
    info = {"last_success": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        info["lake_last_day"] = json.loads(
            (ROOT / "data" / "water_usage.json").read_text(encoding="utf-8")).get("lastDay")
    except Exception:
        pass
    try:
        td = json.loads((ROOT / "data" / "water_tank_daily.json").read_text(encoding="utf-8"))
        info["tank_last_day"] = td.get("lastDay")
        info["tank_total_ml"] = td.get("totalMl")
    except Exception:
        pass
    STATUS_FILE.write_text(json.dumps(info, separators=(",", ":")) + "\n", encoding="utf-8")
    log("heartbeat:", info["last_success"])


def commit_and_push():
    files = ["data/water_usage.json", "data/water_register_daily.json",
             "data/water_register_hourly.json", "data/water_tank_daily.json",
             "data/das_auto_export_status.json"]
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
            str(PROFILE_DIR), headless=not headed, accept_downloads=True,
            viewport={"width": 1680, "height": 1000})
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

            if mode == "run":
                sync_repo()  # pull latest so imports + tank rebuild start current

            with tempfile.TemporaryDirectory() as td:
                zip_path = pathlib.Path(td) / "das-export.zip"
                export_download(page, zip_path)
                import_export_zip(zip_path)

            build_tank_daily()  # yesterday's tank water (sum of dips) -> water_tank_daily.json

            if mode == "run":
                write_status()      # heartbeat for das_watchdog, committed with the data
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
