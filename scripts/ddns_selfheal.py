#!/usr/bin/env python3
"""
DuckDNS self-heal for the Grundfos pump poller.

Runs in the poll-grundfos workflow straight after scripts/poll_grundfos.py.
When the station is offline because the DuckDNS name no longer points at our
USR cellular router (Telstra reissued the SIM's public IP and the router's
own DDNS client failed to push it - the 23 Aug 2026 outage), this script:

  1. Works out the router's CURRENT public IP from the first source that has one:
       a. ROUTER_IP_OVERRIDE    - typed into the workflow_dispatch form by a human
                                  who read it off the Telstra/Jasper portal
       b. Supabase heartbeat    - pump_router_status rows written by the on-site
                                  Pi (pi_pump_poller.py ddns guard), < 30 min old
       c. Jasper Control Center - GET /devices/{iccid} -> ipAddress, if the
                                  JASPER_API_USER / JASPER_API_KEY / JASPER_ICCID
                                  secrets are configured
  2. Proves the candidate really is our station (Modbus :502 answers there,
     or our router's LuCI page answers on 80/443).
  3. Pushes it to DuckDNS with an EXPLICIT ip= (DuckDNS must never auto-detect:
     Telstra's outbound NAT address differs from the assigned, reachable IP).
  4. Records the repair in data/pump_ip_log.json and pump_station_latest.json
     and exports new_ip to $GITHUB_OUTPUT so the workflow can re-poll at once.

Does nothing (exit 0) when the station is online, when the diagnosis says the
problem is not DNS, or when no IP source is configured - in that case it only
prints what a human needs to do.

Env:
  DUCKDNS_TOKEN        DuckDNS account token (repo secret)
  DUCKDNS_DOMAIN       subdomain, default "bidgee-pumps"
  DDNS_HOST            full hostname, default "<DUCKDNS_DOMAIN>.duckdns.org"
  ROUTER_IP_OVERRIDE   manual IP from workflow_dispatch
  SUPABASE_URL/KEY     default to the project's anon key (same as the Pi poller)
  JASPER_API_BASE      default https://restapi.jasper.com/rws/api/v1
  JASPER_API_USER, JASPER_API_KEY, JASPER_ICCID
"""

import base64
import json
import os
import socket
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

DATA_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
LATEST_FILE  = os.path.join(DATA_DIR, "pump_station_latest.json")
IP_LOG_FILE  = os.path.join(DATA_DIR, "pump_ip_log.json")

DUCKDNS_TOKEN  = (os.environ.get("DUCKDNS_TOKEN") or "").strip()
DUCKDNS_DOMAIN = (os.environ.get("DUCKDNS_DOMAIN") or "bidgee-pumps").strip()
DDNS_HOST      = (os.environ.get("DDNS_HOST") or f"{DUCKDNS_DOMAIN}.duckdns.org").strip()
OVERRIDE_IP    = (os.environ.get("ROUTER_IP_OVERRIDE") or "").strip()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://sduzxijjvpbfgvlwcwpp.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkdXp4aWpqdnBiZmd2bHdjd3BwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1ODE2NzgsImV4cCI6MjA5MjE1NzY3OH0.fbYf9-F987DUSlsibuGnqGYEQe6tsQsOf7NMmNMrBT8")
HEARTBEAT_MAX_AGE_MIN = 30

JASPER_API_BASE = os.environ.get("JASPER_API_BASE", "https://restapi.jasper.com/rws/api/v1")
JASPER_USER     = os.environ.get("JASPER_API_USER", "")
JASPER_KEY      = os.environ.get("JASPER_API_KEY", "")
JASPER_ICCID    = os.environ.get("JASPER_ICCID", "")

DNS_CODES_WE_CAN_FIX = {"ip_reassigned", "dns_private", "dns_failed", "unreachable"}
ROUTER_FINGERPRINTS  = ("luci", "usr", "bidgee", "openwrt")


def log(msg):
    print(f"[ddns-selfheal] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def is_ip(s):
    try:
        socket.inet_aton(str(s))
        return str(s).count(".") == 3
    except Exception:
        return False


def http_get(url, headers=None, timeout=12, insecure=False):
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "wwcc-ddns-selfheal/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read(50000).decode("utf-8", "ignore")


def tcp_open(ip, port, timeout=6):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


# ── IP sources ────────────────────────────────────────────────────────────────

def ip_from_override():
    if OVERRIDE_IP:
        if is_ip(OVERRIDE_IP):
            return OVERRIDE_IP, "manual_override"
        log(f"ROUTER_IP_OVERRIDE '{OVERRIDE_IP}' is not a valid IPv4 address - ignored")
    return None, None


def ip_from_supabase_heartbeat():
    """Latest wan_ip the on-site Pi reported, if fresh."""
    try:
        url = (f"{SUPABASE_URL}/rest/v1/pump_router_status"
               f"?select=ts,wan_ip,source&wan_ip=not.is.null&order=ts.desc&limit=1")
        status, body = http_get(url, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "User-Agent": "wwcc-ddns-selfheal/1.0"})
        rows = json.loads(body) if status == 200 else []
        if not rows:
            return None, None
        row = rows[0]
        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        if age > timedelta(minutes=HEARTBEAT_MAX_AGE_MIN):
            log(f"Supabase heartbeat too old ({age.total_seconds()/60:.0f} min) - ignored")
            return None, None
        if is_ip(row.get("wan_ip")):
            return row["wan_ip"], f"pi_heartbeat:{row.get('source')}"
    except Exception as e:
        log(f"Supabase heartbeat lookup failed: {e}")
    return None, None


def ip_from_jasper():
    if not (JASPER_USER and JASPER_KEY and JASPER_ICCID):
        return None, None
    try:
        auth = base64.b64encode(f"{JASPER_USER}:{JASPER_KEY}".encode()).decode()
        url = f"{JASPER_API_BASE.rstrip('/')}/devices/{urllib.parse.quote(JASPER_ICCID)}"
        status, body = http_get(url, headers={
            "Authorization": f"Basic {auth}", "Accept": "application/json",
            "User-Agent": "wwcc-ddns-selfheal/1.0"})
        d = json.loads(body)
        ip = d.get("ipAddress") or d.get("ip_address")
        if is_ip(ip):
            return ip, "jasper_api"
        log(f"Jasper returned no usable ipAddress: {str(d)[:200]}")
    except Exception as e:
        log(f"Jasper API lookup failed: {e}")
    return None, None


def verify_is_our_station(ip):
    """True if Modbus :502 answers (CIM500 behind our router) or our router's
    LuCI page answers. Avoids pointing DuckDNS at a stranger."""
    if tcp_open(ip, 502):
        return True, "modbus:502 open"
    for url in (f"http://{ip}/", f"https://{ip}/"):
        try:
            _, body = http_get(url, timeout=8, insecure=True)
            low = body.lower()
            if any(fp in low for fp in ROUTER_FINGERPRINTS):
                return True, f"router page at {url}"
        except Exception:
            pass
    return False, "no Modbus and no router page at that IP"


def duckdns_update(ip):
    url = (f"https://www.duckdns.org/update?domains={urllib.parse.quote(DUCKDNS_DOMAIN)}"
           f"&token={urllib.parse.quote(DUCKDNS_TOKEN)}&ip={ip}")
    status, body = http_get(url, timeout=15)
    return body.strip().upper() == "OK", body.strip()


def gh_output(key, val):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{key}={val}\n")


def main():
    latest = load_json(LATEST_FILE, {})
    if latest.get("connected"):
        log("station online - nothing to do")
        return 0

    diag = latest.get("offline_diagnosis") or {}
    code = diag.get("code", "unknown")
    dns_ip = diag.get("dns_ip")
    log(f"station offline - diagnosis: {code}: {diag.get('summary', '')}")

    if code not in DNS_CODES_WE_CAN_FIX and not OVERRIDE_IP:
        log("not a DNS problem (or unknown) - no DuckDNS repair attempted")
        return 0

    candidate, source = None, None
    for fn in (ip_from_override, ip_from_supabase_heartbeat, ip_from_jasper):
        candidate, source = fn()
        if candidate:
            log(f"candidate IP {candidate} from {source}")
            break

    now_iso = datetime.now(timezone.utc).isoformat()
    result = {"checked_at": now_iso, "candidate_ip": candidate, "source": source,
              "dns_ip": dns_ip, "action": "none", "detail": ""}

    if not candidate:
        result["detail"] = ("no IP source available - configure the Pi heartbeat "
                            "(on-site), JASPER_* secrets, or re-run this workflow "
                            "with router_ip from the Telstra/Jasper portal")
        log(result["detail"])
    elif candidate == dns_ip:
        result["action"] = "dns_already_correct"
        result["detail"] = "DuckDNS already points at the candidate IP - outage is not DNS"
        log(result["detail"])
    else:
        ok, why = verify_is_our_station(candidate)
        if not ok:
            result["action"] = "candidate_rejected"
            result["detail"] = f"{candidate} rejected: {why}"
            log(result["detail"])
        elif not DUCKDNS_TOKEN:
            result["action"] = "token_missing"
            result["detail"] = (f"{candidate} verified ({why}) but DUCKDNS_TOKEN secret is not set - "
                                f"update manually: https://www.duckdns.org/update?domains="
                                f"{DUCKDNS_DOMAIN}&token=YOUR_TOKEN&ip={candidate}")
            log(result["detail"])
        else:
            try:
                updated, body = duckdns_update(candidate)
            except Exception as e:
                updated, body = False, str(e)
            if updated:
                result["action"] = "duckdns_updated"
                result["detail"] = f"DuckDNS {DDNS_HOST} repointed {dns_ip} -> {candidate} ({why})"
                log(result["detail"])
                iplog = load_json(IP_LOG_FILE, [])
                if not isinstance(iplog, list):
                    iplog = []
                iplog.insert(0, {"ts": now_iso, "old_ip": dns_ip, "new_ip": candidate,
                                 "healed": "auto", "source": source})
                write_json(IP_LOG_FILE, iplog[:100])
                gh_output("new_ip", candidate)
            else:
                result["action"] = "duckdns_failed"
                result["detail"] = f"DuckDNS update returned '{body}'"
                log(result["detail"])

    latest["selfheal"] = result
    write_json(LATEST_FILE, latest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
