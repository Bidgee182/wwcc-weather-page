#!/usr/bin/env python3
"""
Shared outbound mailer for every WWCC email - Resend REST API.

Replaced SendGrid on 24 Aug 2026: the SendGrid free trial expired on 23 Aug
and every send started returning 401. The domain send.wwcc.com.au is verified
in Resend (same account Navigate9 uses), so any local-part on it can be a
From address - each area of the site sends under its own identity.

Usage from the report/alert scripts:

    from mailer import send_html
    ok, detail = send_html(subject, html, to_list, cc_list, bcc_list,
                           stream='pump', text=plain_text)

- `stream` picks the From identity from data/email_config.json "senders"
  (plain config, editable from the admin page - not a secret). Falls back to
  the built-in defaults below.
- Reply-To defaults to senders.default_reply_to so replies land in a real
  mailbox. (Mail sent TO @send.wwcc.com.au goes to Navigate9's inbound
  webhook, so the From addresses themselves are not monitored inboxes.)
- RESEND_API_KEY (env / repo secret) is the only credential.
- Returns (ok, detail); detail is "sent (200) ..." / "failed: ..." shaped for
  lake_utils.log_email.

Self-test (really sends):  python scripts/mailer.py --test you@example.com
"""

import json
import os
import sys
import urllib.request
from urllib.error import HTTPError

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_URL     = "https://api.resend.com/emails"

_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "data", "email_config.json")

_DEFAULT_SENDERS = {
    "pump":     "WWCC Pump Station <pump@send.wwcc.com.au>",
    "weather":  "WWCC Weather <weather@send.wwcc.com.au>",
    "lake":     "WWCC Lake Albert <lake@send.wwcc.com.au>",
    "board":    "WWCC Board Dashboard <board@send.wwcc.com.au>",
    "watchdog": "WWCC Automation <watchdog@send.wwcc.com.au>",
    "default":  "WWCC Weather <weather@send.wwcc.com.au>",
    "default_reply_to": "andrew@bidgeepumps.com.au",
}


def _senders():
    """Sender map: built-in defaults overlaid with data/email_config.json."""
    merged = dict(_DEFAULT_SENDERS)
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in (cfg.get("senders") or {}).items():
            if v:
                merged[k] = v
    except Exception:
        pass
    return merged


def sender_for(stream):
    s = _senders()
    return s.get(stream or "default") or s["default"]


def _clean(addrs):
    return [a.strip() for a in (addrs or []) if a and a.strip() and "@" in a]


def send_html(subject, html, to, cc=None, bcc=None, stream="default",
              text=None, reply_to=None, from_addr=None):
    """Send one email via Resend. Returns (ok: bool, detail: str)."""
    to, cc, bcc = _clean(to), _clean(cc), _clean(bcc)
    if not RESEND_API_KEY:
        return False, "failed: RESEND_API_KEY not set"
    if not to:
        return False, "failed: no To recipients"

    payload = {
        "from":    from_addr or sender_for(stream),
        "to":      to,
        "subject": subject,
    }
    if html:
        payload["html"] = html
    if text:
        payload["text"] = text
    if not html and not text:
        return False, "failed: no body"
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    rt = reply_to if reply_to is not None else _senders().get("default_reply_to")
    if rt:
        payload["reply_to"] = rt

    req = urllib.request.Request(
        RESEND_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8", "ignore") or "{}")
            return True, f"sent ({r.status}) id {body.get('id', '?')}"
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            detail = ""
        return False, f"failed: HTTP {e.code}: {detail}"
    except Exception as e:
        return False, f"failed: {e}"


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        addr = sys.argv[2]
        ok, detail = send_html(
            "WWCC mailer self-test (Resend)",
            "<p>This is the WWCC shared mailer confirming the switch from "
            "SendGrid to <b>Resend</b> (send.wwcc.com.au). Per-stream senders "
            "are active - this one went out as the <i>weather</i> identity.</p>",
            [addr], stream="weather",
            text="WWCC shared mailer self-test - Resend switch confirmed.")
        print(("OK: " if ok else "FAIL: ") + detail)
        sys.exit(0 if ok else 1)
    print("usage: python mailer.py --test recipient@example.com")
