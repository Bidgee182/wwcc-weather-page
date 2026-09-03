#!/usr/bin/env python3
"""Leaderboard watchdog: keeps the TV leaderboard honest and emails Andrew.

Runs every 15 min from .github/workflows/leaderboard-watchdog.yml during the
same daylight window as the poller. Reads the published kiosk JSON, re-fetches
the public MiScore board as an independent cross-check, and:

  - self-heals a dead poll chain (re-dispatches leaderboard-poll.yml)
  - emails an ALERT when something is broken and could not be healed
    (persisting 2 consecutive checks, re-notify every 2 h while broken)
  - emails RESOLVED when a previously alerted problem clears
  - emails a RESULTS CONFIRMATION the first time each comp's official PDF
    results are parsed (grades, NTP / Longest Drive / ball-winner counts,
    plus any gaps found)
  - writes out/lb-health.json for the admin dashboard health card

State + dedup live in out/lb-watchdog-state.json (committed by the workflow).
All emails go through scripts/mailer.py (Resend) as the watchdog identity and
are logged to data/email_log.csv like every other WWCC send.
"""
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from mailer import send_html          # noqa: E402
from lake_utils import log_email      # noqa: E402

LIVE_PATH    = ROOT / "out" / "live-leaderboard.json"
RESULTS_PATH = ROOT / "out" / "last-results.json"
STATE_PATH   = ROOT / "out" / "lb-watchdog-state.json"
HEALTH_PATH  = ROOT / "out" / "lb-health.json"

ADMINS       = ["andrew@bidgeepumps.com.au"]
TZ           = ZoneInfo("Australia/Sydney")
LAT, LON     = -35.12, 147.37
WINDOW_MARGIN_MIN = 240          # same as the poller: 4 h past sunset

STALE_MIN        = 18            # poll cadence ~1 min; cron fallback restarts in <=10
REALERT_MIN      = 120           # re-notify cadence while a problem persists
PLAYER_DROP_PCT  = 0.65          # alert when field shrinks below 65% of today's max
MISMATCH_ALLOWED = 2             # top-10 rows allowed to differ from a fresh scrape

GH_REPO  = os.environ.get("GH_REPO", "Bidgee182/wwcc-weather-page")
GH_TOKEN = os.environ.get("GH_TOKEN", "")


# ── window ────────────────────────────────────────────────────────────────────

def in_daylight_window(now=None):
    """Same sunrise-4h .. sunset+4h window the poll workflow gates on."""
    now = now or datetime.now(TZ)
    n = now.timetuple().tm_yday
    d = math.asin(math.sin(math.radians(23.45)) * math.sin(math.radians(360 / 365 * (n - 81))))
    cos_h = ((math.sin(math.radians(-0.833)) - math.sin(math.radians(LAT)) * math.sin(d))
             / (math.cos(math.radians(LAT)) * math.cos(d)))
    h_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_h))))
    B = math.radians(360 / 365 * (n - 81))
    eot_min = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    noon_local = 12 - LON / 15 - eot_min / 60 + now.utcoffset().total_seconds() / 3600
    rise, sset = noon_local - h_deg / 15, noon_local + h_deg / 15
    cur = now.hour + now.minute / 60
    return (rise - WINDOW_MARGIN_MIN / 60) <= cur <= (sset + WINDOW_MARGIN_MIN / 60)


# ── helpers ───────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def utcnow():
    return datetime.now(timezone.utc)


def parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def dispatch_poll_workflow():
    """Self-heal: kick the poll chain via the GitHub API."""
    if not GH_TOKEN:
        return False, "no GH_TOKEN"
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/actions/workflows/leaderboard-poll.yml/dispatches",
        data=json.dumps({"ref": "main"}).encode(),
        headers={"Authorization": "Bearer " + GH_TOKEN,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "wwcc-lb-watchdog"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 204), f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)


def fresh_board_top10(board_id):
    """Independent cross-check: fetch the public MiScore board page now and
    return its top-10 (name, points/score) rows. None on any failure."""
    try:
        from miscore.live import _board_players
        from miscore.webscrape import BASE, _get
        page = _get(f"{BASE}/display-leaderboard?club=wwcc&leaderboardId={board_id}")
        players = _board_players(page) or []
        out = []
        for p in players[:10]:
            out.append((_norm(p.get("player")), p.get("points")))
        return out
    except Exception as e:
        print(f"cross-check fetch failed (non-fatal): {e}")
        return None


def _norm(name):
    return re.sub(r"[^a-z]", "", str(name or "").lower())


def fetch_board_list():
    """What does MiScore itself list for the club today?
    Returns (status, board_ids): status is 'ok', 'org-missing' (the club slug
    no longer resolves - a MiClub-side rename/outage) or 'error: ...'."""
    try:
        from miscore.webscrape import BASE, _get
        page = _get(f"{BASE}/show-leaderboards?club=wwcc&days=1")
        if "Organisation doesn" in page or "doesn&#039;t exist" in page:
            return "org-missing", []
        return "ok", sorted(set(re.findall(r"leaderboardId=(\d+)", page)))
    except Exception as e:
        return f"error: {e}", []


# ── the checks ────────────────────────────────────────────────────────────────

def run_checks(live, state, within):
    """Return (issues dict key->message, health check rows)."""
    issues, rows = {}, []

    def row(key, label, status, detail):
        rows.append({"key": key, "label": label, "status": status, "detail": detail})

    gen = parse_iso(live.get("generatedAt"))
    age_min = (utcnow() - gen).total_seconds() / 60 if gen else 9999
    state["pollAgeMin"] = round(age_min, 1)

    # 0. Source check: what does MiScore itself say? This runs even when the
    # published JSON is empty - the blind spot that hid the 3 Sep 2026 outage,
    # where the wwcc organisation vanished from leaderboard.miclub.com.au and
    # the board silently showed "no comps" while two ladies comps were on.
    if within:
        src_status, src_ids = fetch_board_list()
        if src_status == "org-missing":
            issues["orgmissing"] = ("MiScore no longer recognises the club: "
                                    "leaderboard.miclub.com.au returns \"Organisation doesn't exist\" "
                                    "for club=wwcc. Nothing can be scraped until MiClub restores or "
                                    "renames the organisation - contact MiClub support.")
            row("source", "MiScore club page", "crit", "Organisation doesn't exist")
        elif src_status == "ok" and src_ids and not live.get("competition"):
            issues["nocomp"] = (f"MiScore lists {len(src_ids)} board(s) for today but the published "
                                f"leaderboard has no competition - the poller is not picking them up.")
            row("source", "MiScore club page", "crit", f"{len(src_ids)} boards listed, none published")
        elif src_status == "ok":
            row("source", "MiScore club page", "ok",
                f"{len(src_ids)} board(s) listed" if src_ids else "No comps listed today")
        else:
            row("source", "MiScore club page", "idle", f"Unreachable this check ({src_status[:60]})")
    else:
        row("source", "MiScore club page", "idle", "Outside daylight window")

    # 1. Freshness / chain alive
    if not within:
        row("stale", "Poller freshness", "idle", "Outside daylight window")
    elif age_min > STALE_MIN:
        issues["stale"] = (f"Leaderboard JSON is {age_min:.0f} min old "
                           f"(expected ~1 min during the day). The poll chain looks dead.")
        row("stale", "Poller freshness", "crit", f"{age_min:.0f} min old")
    else:
        row("stale", "Poller freshness", "ok", f"Updated {age_min:.0f} min ago")

    # 2. Credentials present (results PDFs need the WWCC login)
    if live.get("wwccCredSet") is False:
        issues["creds"] = "WWCC_USERNAME / WWCC_PASSWORD secrets are missing or empty - PDF results cannot be read."
        row("creds", "WWCC login secrets", "crit", "Not set")
    else:
        row("creds", "WWCC login secrets", "ok", "Present")

    started = bool(live.get("started"))
    count   = int(live.get("playerCount") or 0)
    today   = str(live.get("date") or "")

    # daily max resets when the comp date changes
    if state.get("day") != today:
        state["day"] = today
        state["maxPlayers"] = 0
        state["resultsEmailed"] = state.get("resultsEmailed") or {}
    state["maxPlayers"] = max(int(state.get("maxPlayers") or 0), count)

    # 3. Board emptied / shrank during a live comp
    if started and within and count == 0:
        issues["empty"] = "A comp is running but the published board has 0 players (scrape or login failure)."
        row("players", "Field size", "crit", "0 players while comp live")
    elif started and state["maxPlayers"] >= 20 and count < state["maxPlayers"] * PLAYER_DROP_PCT:
        issues["drop"] = (f"Player count dropped to {count} from {state['maxPlayers']} today - "
                          f"possible partial scrape.")
        row("players", "Field size", "warn", f"{count} (peak {state['maxPlayers']})")
    else:
        row("players", "Field size", "ok", f"{count} players")

    # 4. Accuracy: compare published top-10 against a fresh scrape of MiScore
    if within and started and count > 0 and age_min <= 6 and live.get("leaderboardId"):
        fresh = fresh_board_top10(live["leaderboardId"])
        if fresh:
            pub = {}
            for p in (live.get("players") or [])[:15]:
                pub[_norm(p.get("player"))] = p.get("points")
            diffs = [n for n, pts in fresh if n and (n not in pub or pub[n] != pts)]
            if len(diffs) > MISMATCH_ALLOWED:
                issues["mismatch"] = (f"Published board disagrees with a fresh MiScore scrape on "
                                      f"{len(diffs)} of the top 10 (e.g. {diffs[:3]}).")
                row("accuracy", "Matches MiScore", "warn", f"{len(diffs)}/10 rows differ")
            else:
                row("accuracy", "Matches MiScore", "ok", "Top 10 verified against MiScore")
        else:
            row("accuracy", "Matches MiScore", "idle", "MiScore fetch unavailable this check")
    else:
        row("accuracy", "Matches MiScore", "idle", "Checked while comp is live")

    # 5. Official PDF results parsed properly
    ready = bool(live.get("officialResultsReady"))
    ps = live.get("pdfStandings") or {}
    if ready:
        grades  = ps.get("grades") or []
        pplayers = ps.get("players") or []
        if not grades or not pplayers:
            issues["pdf"] = ("Official results are published on the club site but the PDF parse "
                             "came back empty - standings/results mode will be wrong on the TV.")
            row("pdf", "Results PDF", "crit", "Published but parsed empty")
        else:
            row("pdf", "Results PDF", "ok", f"{len(grades)} grades, {len(pplayers)} players parsed")
        if not (live.get("ntpLd") or []):
            issues["ntpld"] = "Results are official but no NTP / Longest Drive entries were parsed from the PDF."
            row("ntpld", "NTP / Longest Drive", "warn", "None parsed")
        else:
            n_ntp = sum(1 for e in live["ntpLd"] if e.get("type") == "ntp")
            n_ld  = len(live["ntpLd"]) - n_ntp
            row("ntpld", "NTP / Longest Drive", "ok", f"{n_ntp} NTP, {n_ld} LD")
    else:
        row("pdf", "Results PDF", "idle", "Awaiting official results")
        row("ntpld", "NTP / Longest Drive", "idle", "Awaiting official results")

    return issues, rows


# ── emails ────────────────────────────────────────────────────────────────────

def _send(subject, html, text):
    ok, detail = send_html(subject, html, ADMINS, stream="watchdog", text=text)
    log_email("lb_watchdog", subject, ADMINS, detail)
    msg = ("sent: " if ok else "SEND FAILED: ") + subject + " - " + detail
    print(msg.encode("ascii", "replace").decode())  # console-safe on Windows
    return ok


def email_alert(issues, healed_note):
    items = "".join(f"<li><b>{k}</b>: {v}</li>" for k, v in issues.items())
    heal = f"<p><i>Self-heal: {healed_note}</i></p>" if healed_note else ""
    _send("🔴 Leaderboard watchdog: " + ", ".join(issues.keys()),
          f"<h3>WWCC TV leaderboard has a problem</h3><ul>{items}</ul>{heal}"
          f"<p>Live page: <a href='https://bidgee182.github.io/wwcc-weather-page/leaderboard.html'>"
          f"leaderboard.html</a> · Health card is on the admin dashboard.</p>",
          "Leaderboard issues: " + "; ".join(f"{k}: {v}" for k, v in issues.items()))


def email_resolved(cleared):
    _send("✅ Leaderboard watchdog: resolved - " + ", ".join(cleared),
          "<p>These leaderboard problems have cleared and the board is healthy again:</p><ul>"
          + "".join(f"<li>{c}</li>" for c in cleared) + "</ul>",
          "Resolved: " + ", ".join(cleared))


def email_results_confirmation(live):
    ps      = live.get("pdfStandings") or {}
    ntp_ld  = live.get("ntpLd") or []
    n_ntp   = sum(1 for e in ntp_ld if e.get("type") == "ntp")
    n_ld    = len(ntp_ld) - n_ntp
    balls   = live.get("ballWinners") or []
    grades  = ps.get("grades") or []
    warn    = []
    if not grades:            warn.append("No grades parsed from the PDF")
    if not ntp_ld:            warn.append("No NTP / Longest Drive entries parsed")
    if not balls:             warn.append("No ball winners parsed")
    ntp_rows = "".join(f"<li>{'NTP' if e.get('type') == 'ntp' else 'Long Drive'} hole {e.get('hole')}: "
                       f"{e.get('winner')} ({e.get('distance')})</li>" for e in ntp_ld)
    warn_html = ("<p style='color:#c0392b'><b>Gaps:</b> " + "; ".join(warn) + "</p>") if warn else ""
    return _send(f"📄 Results read OK - {live.get('competition', 'comp')} {live.get('date', '')}",
          f"<h3>Official results parsed for {live.get('competition')}</h3>"
          f"<p>{len(grades)} grades ({', '.join(map(str, grades))}) · "
          f"{len(ps.get('players') or [])} players in PDF standings · "
          f"{n_ntp} NTP · {n_ld} Longest Drive · {len(balls)} ball winners.</p>"
          f"<ul>{ntp_rows}</ul>{warn_html}",
          f"Results parsed: {len(grades)} grades, {n_ntp} NTP, {n_ld} LD, {len(balls)} balls. "
          + ("Gaps: " + "; ".join(warn) if warn else "No gaps."))


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    live  = load_json(LIVE_PATH)
    state = load_json(STATE_PATH)
    if not live:
        print("no live-leaderboard.json - nothing to check")
        return 0

    within = in_daylight_window()
    issues, rows = run_checks(live, state, within)

    now_iso = utcnow().isoformat()
    active  = state.get("active") or {}   # key -> {since, lastMail, msg}
    healed_note = None

    # Self-heal a dead chain before deciding to alert
    if "stale" in issues:
        ok, detail = dispatch_poll_workflow()
        healed_note = f"re-dispatched leaderboard-poll.yml ({detail})"
        print("self-heal:", healed_note)

    # Persistence + cooldown: a problem must survive 2 consecutive checks to
    # email, and re-notifies at most every REALERT_MIN while it lasts.
    pending = state.get("pending") or {}
    to_mail = {}
    IMMEDIATE = {"orgmissing", "nocomp"}   # unambiguous hard failures: no 2-check wait
    for k, msg in issues.items():
        if k in IMMEDIATE and k not in active:
            to_mail[k] = msg
            active[k] = {"since": now_iso, "lastMail": now_iso, "msg": msg}
            continue
        if k in active:
            last = parse_iso(active[k].get("lastMail"))
            if last and (utcnow() - last) > timedelta(minutes=REALERT_MIN):
                to_mail[k] = msg + " (still broken)"
                active[k]["lastMail"] = now_iso
            active[k]["msg"] = msg
        elif k in pending:
            to_mail[k] = msg
            active[k] = {"since": now_iso, "lastMail": now_iso, "msg": msg}
        # first sighting -> goes to pending, no email yet
    state["pending"] = {k: v for k, v in issues.items() if k not in active}

    cleared = [active[k]["msg"] for k in list(active) if k not in issues]
    for k in list(active):
        if k not in issues:
            del active[k]
    state["active"] = active

    if to_mail:
        email_alert(to_mail, healed_note)
    if cleared:
        email_resolved(cleared)

    # Results confirmation - once per comp (keyed by date + board id)
    if live.get("officialResultsReady"):
        key = f"{live.get('date')}:{live.get('leaderboardId')}"
        sent_map = state.get("resultsEmailed") or {}
        if key not in sent_map:
            # only mark sent on success - a transient send failure retries next check
            if email_results_confirmation(live):
                sent_map[key] = now_iso
                state["resultsEmailed"] = sent_map

    # Health card for the admin dashboard
    ntp_ld = live.get("ntpLd") or []
    health = {
        "updatedAt": now_iso,
        "window": "day" if within else "night",
        "alertsActive": sorted(active.keys()),
        "competition": live.get("competition"),
        "compDate": live.get("date"),
        "playerCount": live.get("playerCount"),
        "pollAgeMin": state.get("pollAgeMin"),
        "officialResultsReady": bool(live.get("officialResultsReady")),
        "prizes": {"ntp": sum(1 for e in ntp_ld if e.get("type") == "ntp"),
                   "ld":  sum(1 for e in ntp_ld if e.get("type") != "ntp"),
                   "balls": len(live.get("ballWinners") or [])},
        "checks": rows,
    }
    HEALTH_PATH.write_text(json.dumps(health, indent=1), encoding="utf-8")
    state["lastRun"] = now_iso
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")

    print(f"checks done - {len(issues)} issue(s), {len(to_mail)} mailed, "
          f"{len(cleared)} resolved, window={'day' if within else 'night'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
