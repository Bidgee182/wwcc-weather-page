#!/usr/bin/env python3
"""Watchdog for the daily WaterNSW DAS auto-export.

Runs on GitHub Actions (independent of andrew-new), so it still fires when that PC is
asleep, Playwright hangs, or git push is blocked - the cases the in-job login-expiry
email cannot catch. It reads data/das_auto_export_status.json (the heartbeat
das_auto_export.py commits on every successful run) from the repo checkout and emails
Andrew if the last success is older than the threshold (default 48h) or the heartbeat
is missing/unreadable.

    python scripts/das_watchdog.py            # alert only if stale (needs RESEND_API_KEY)
    python scripts/das_watchdog.py --test     # send a [TEST] alert regardless
    python scripts/das_watchdog.py --hours 72 # change the staleness threshold
"""
import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATUS = ROOT / "data" / "das_auto_export_status.json"
STALE_HOURS = 48


def main():
    ap = argparse.ArgumentParser(description="Alert if the DAS auto-export has stalled")
    ap.add_argument("--test", action="store_true", help="send a [TEST] alert regardless")
    ap.add_argument("--hours", type=int, default=STALE_HOURS, help="staleness threshold")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    last, age_h, reason = None, None, None
    if not STATUS.exists():
        reason = "the heartbeat file data/das_auto_export_status.json is missing"
    else:
        try:
            blob = json.loads(STATUS.read_text(encoding="utf-8"))
            last = datetime.fromisoformat(str(blob.get("last_success")).replace("Z", "+00:00"))
        except Exception as e:
            reason = f"the heartbeat could not be read ({e})"
    if last is not None:
        age_h = (now - last).total_seconds() / 3600
        if age_h > args.hours:
            stamp = last.strftime("%Y-%m-%d %H:%M")
            reason = f"the last successful run was {age_h:.0f}h ago ({stamp} UTC)"

    stale = reason is not None
    if not stale and not args.test:
        print(f"DAS auto-export healthy (last success {age_h:.1f}h ago). No alert.")
        return 0

    sys.path.insert(0, str(ROOT / "scripts"))
    import mailer
    from das_auto_export import ALERT_TO

    prefix = ""
    if args.test and not stale:
        prefix = "[TEST] "
        reason = reason or f"(test) last success {age_h:.1f}h ago"
    checked = now.strftime("%Y-%m-%d %H:%M")
    html = (
        "<p>The automated WaterNSW DAS export has not reported a successful run recently.</p>"
        f"<p><b>Problem:</b> {reason}.</p>"
        f"<p>Threshold {args.hours}h; checked {checked} UTC.</p>"
        "<p>On andrew-new: confirm the PC is on and logged in, then check "
        "<code>C:\\Apps\\das_auto_export.log</code>. Run it now with "
        "<code>python scripts/das_auto_export.py</code>, or re-seed the login with "
        "<code>python scripts/das_auto_export.py --login</code>.</p>"
    )
    ok, detail = mailer.send_html(
        prefix + "DAS auto-export watchdog: daily import has stalled",
        html, ALERT_TO, stream="default")
    print("watchdog alert:", "sent" if ok else detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
