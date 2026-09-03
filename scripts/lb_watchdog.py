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
RESULTS_LOG  = ROOT / "data" / "results_log.csv"

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
    """Independent cross-check: fetch the MiScore board now (public host,
    guest API fallback) and return top-10 (name, points) rows. None on failure."""
    try:
        from miscore.live import _board_players
        from miscore.webscrape import BASE, _get
        page = _get(f"{BASE}/display-leaderboard?club=wwcc&leaderboardId={board_id}")
        if "Organisation doesn" not in page and "doesn&#039;t exist" not in page:
            players = _board_players(page) or []
            # boardTotal is the board page's Total column (points / net score)
            return [(_norm(p.get("player")), p.get("boardTotal")) for p in players[:10]]
    except Exception:
        pass
    try:
        from miscore.guestapi import guest_board
        gb = guest_board("wwcc", str(board_id))
        if not gb:
            return None
        return [(_norm(p.get("player")), p.get("boardTotal")) for p in gb["field"][:10]]
    except Exception as e:
        print(f"cross-check fetch failed (non-fatal): {e}")
        return None


def _norm(name):
    return re.sub(r"[^a-z]", "", str(name or "").lower())


# ── results audit log (data/results_log.csv, shows in admin Audit Logs) ───────

def all_boards(live):
    """[(board_dict, role)] - the primary blob plus each companion."""
    boards = [(live, "primary")]
    for c in live.get("companions") or []:
        boards.append((c, "companion"))
    return boards


def classify_results(b):
    """(status, summary) for a board with officialResultsReady=True."""
    ps = b.get("pdfStandings") or {}
    grades, players = ps.get("grades") or [], ps.get("players") or []
    ntp_ld = b.get("ntpLd") or []
    n_ntp = sum(1 for e in ntp_ld if e.get("type") == "ntp")
    n_ld = len(ntp_ld) - n_ntp
    balls = b.get("ballWinners") or []
    if not grades and not players:
        status = "failed"
    elif not ntp_ld or not balls or not grades:
        status = "partial"
    else:
        status = "success"
    summary = f"{len(grades)} grades, {len(players)} players, {n_ntp} NTP, {n_ld} LD, {len(balls)} balls"
    return status, summary


def append_results_log(comp_date, competition, board_id, role, status, summary, note=""):
    """One audit row per comp per day. Never raises."""
    import csv
    try:
        new = not RESULTS_LOG.exists() or RESULTS_LOG.stat().st_size == 0
        with RESULTS_LOG.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp_utc", "comp_date", "competition", "leaderboard_id",
                            "role", "status", "summary", "note"])
            w.writerow([utcnow().isoformat(), comp_date, competition, board_id,
                        role, status, summary, note])
    except Exception as e:
        print(f"results log write failed: {e}")


def read_results_log_for(comp_date):
    """Rows from the results log for one comp date (newest first)."""
    import csv
    try:
        with RESULTS_LOG.open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("comp_date") == comp_date]
        return list(reversed(rows))
    except Exception:
        return []


def fetch_board_list():
    """What does MiScore itself list for the club today?
    Returns (status, board_ids). status: 'ok' (public host fine),
    'guest' (public host lost the org but the guest JSON API works - the
    poller's fallback source since 3 Sep 2026), 'org-missing' (both dead)
    or 'error: ...'."""
    from datetime import date
    today = date.today().isoformat()

    def _todays(comps):
        return sorted(c["leaderboardId"] for c in comps if c.get("date") == today)

    public_dead = False
    try:
        from miscore.webscrape import BASE, _get, list_competitions
        page = _get(f"{BASE}/show-leaderboards?club=wwcc&days=1")
        if "Organisation doesn" in page or "doesn&#039;t exist" in page:
            public_dead = True
        else:
            # days=1 covers today AND yesterday - count only today's boards
            return "ok", _todays(list_competitions("wwcc", 1))
    except Exception:
        public_dead = True
    try:
        from miscore.guestapi import guest_list_competitions
        return "guest", _todays(guest_list_competitions("wwcc", 1))
    except Exception as e:
        return ("org-missing" if public_dead else f"error: {e}"), []


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
            issues["orgmissing"] = ("MiScore is unreachable on BOTH sources: the public host says "
                                    "\"Organisation doesn't exist\" for club=wwcc AND the guest JSON "
                                    "API on wwcc.miclub.com.au is not answering. Nothing can be "
                                    "scraped - contact MiClub support.")
            row("source", "MiScore club page", "crit", "Public host and guest API both dead")
        elif src_status in ("ok", "guest") and src_ids and not live.get("competition"):
            issues["nocomp"] = (f"MiScore lists {len(src_ids)} board(s) for today but the published "
                                f"leaderboard has no competition - the poller is not picking them up.")
            row("source", "MiScore club page", "crit", f"{len(src_ids)} boards listed, none published")
        elif src_status == "guest":
            row("source", "MiScore club page", "warn",
                (f"{len(src_ids)} board(s) via guest API" if src_ids else "No comps today (guest API)")
                + " - public host still missing the org")
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

            def _differs(name, pts):
                if name not in pub:
                    return True
                if pts is None or pub[name] is None:
                    return False   # source didn't carry a score - can't compare
                try:
                    return abs(float(pts) - float(pub[name])) > 0.01
                except (TypeError, ValueError):
                    return False
            diffs = [n for n, pts in fresh if n and _differs(n, pts)]
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

    # 5. Official PDF results - checked for EVERY comp (primary + companions)
    boards = all_boards(live)
    multi = len(boards) > 1
    for b, role in boards:
        comp = b.get("competition") or role
        short = (comp[:26] + "…") if len(comp) > 27 else comp
        label = f"Results: {short}" if multi else "Results PDF"
        key = f"pdf:{b.get('leaderboardId')}"
        if not b.get("competition"):
            continue
        if bool(b.get("officialResultsReady")):
            status, summary = classify_results(b)
            if status == "failed":
                issues[key] = (f"{comp}: official results published but the PDF parse came back "
                               f"empty - results mode will be wrong on the TV.")
                row(key, label, "crit", "Published but parsed empty")
            elif status == "partial":
                issues[key] = f"{comp}: results parsed with gaps ({summary})."
                row(key, label, "warn", summary)
            else:
                row(key, label, "ok", summary)
        else:
            row(key, label, "idle",
                "Awaiting official results" if b.get("started") else "Comp not started")

    # Yesterday's outcome from the audit log (shown on the health pill)
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


def email_daily_report(live, state):
    """Evening observation summary: every comp seen today, what showed on the
    TV, results-parse outcomes, and any alerts raised during the day."""
    comp_date = str(live.get("date") or datetime.now(TZ).date().isoformat())
    rows_html = []
    for b, role in all_boards(live):
        if not b.get("competition"):
            continue
        if b.get("officialResultsReady"):
            status, summary = classify_results(b)
            icon = {"success": "✅", "partial": "⚠️", "failed": "❌"}.get(status, "•")
            res = f"{icon} {status} - {summary}"
        elif b.get("started"):
            res = "❌ no official results published"
        else:
            res = "comp did not start"
        rows_html.append(f"<li><b>{b.get('competition')}</b> ({role}, "
                         f"{b.get('playerCount') or len(b.get('players') or [])} players): {res}</li>")
    for s in live.get("companionsSkipped") or []:
        rows_html.append(f"<li><b>{s.get('name')}</b>: NOT shown on TV - {s.get('reason')}</li>")

    alerts = (state.get("dayAlerts") or {}).get(comp_date) or []
    alerts_html = ("<p><b>Alerts raised today:</b></p><ul>"
                   + "".join(f"<li>{a}</li>" for a in alerts) + "</ul>") if alerts else \
                  "<p>No alerts raised today.</p>"

    src_status, src_ids = fetch_board_list()
    src_note = {"ok": "public leaderboard host", "guest": "guest API fallback (public host missing the org!)"}\
        .get(src_status, src_status)

    return _send(f"📊 Leaderboard daily report - {comp_date}",
                 f"<h3>TV leaderboard - {comp_date}</h3>"
                 f"<p>{len(src_ids)} board(s) listed by MiScore today · data source: {src_note}</p>"
                 f"<ul>{''.join(rows_html) or '<li>No comps today.</li>'}</ul>"
                 f"{alerts_html}"
                 f"<p>Full history: data/results_log.csv (admin page → Audit Logs).</p>",
                 f"Leaderboard daily report {comp_date}: {len(rows_html)} comp entries, "
                 f"{len(alerts)} alerts, source {src_status}.")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    # Piggyback runs (triggered by each poll-chain completion) only do real
    # work when the last check has gone stale; cron/manual runs always run.
    if os.environ.get("GH_EVENT") == "workflow_run":
        last = parse_iso((load_json(HEALTH_PATH) or {}).get("updatedAt"))
        if last and (utcnow() - last) < timedelta(minutes=10):
            print("skip: checked "
                  f"{(utcnow() - last).total_seconds() / 60:.0f} min ago (piggyback run)")
            return 0

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
        day_key = str(live.get("date") or utcnow().date().isoformat())
        day_alerts = state.get("dayAlerts") or {}
        day_alerts.setdefault(day_key, [])
        day_alerts[day_key] += [f"{k}: {v[:120]}" for k, v in to_mail.items()]
        state["dayAlerts"] = {k: v for k, v in day_alerts.items() if k >= day_key[:8]}
    if cleared:
        email_resolved(cleared)

    # Results confirmation + audit log - once per comp per day, every board
    comp_date = str(live.get("date") or "")
    sent_map = state.get("resultsEmailed") or {}
    logged_map = state.get("resultsLogged") or {}
    for b, role in all_boards(live):
        if not (b.get("competition") and b.get("officialResultsReady")):
            continue
        key = f"{comp_date}:{b.get('leaderboardId')}"
        if key not in logged_map:
            status, summary = classify_results(b)
            append_results_log(comp_date, b.get("competition"), b.get("leaderboardId"),
                               role, status, summary)
            logged_map[key] = now_iso
            state["resultsLogged"] = logged_map
        if key not in sent_map:
            # only mark sent on success - a transient send failure retries next check
            if email_results_confirmation(b):
                sent_map[key] = now_iso
                state["resultsEmailed"] = sent_map

    # Daily observation report - once per day from 19:30 local
    local_now = datetime.now(TZ)
    if local_now.hour + local_now.minute / 60 >= 19.5:
        daily = state.get("dailyEmailed") or {}
        today_local = local_now.date().isoformat()
        if today_local not in daily:
            # comps that ran but never published results get a log row too
            for b, role in all_boards(live):
                if not b.get("competition"):
                    continue
                key = f"{comp_date}:{b.get('leaderboardId')}"
                if key not in (state.get("resultsLogged") or {}) and b.get("started"):
                    append_results_log(comp_date, b.get("competition"), b.get("leaderboardId"),
                                       role, "none-published", "no official results by 7:30pm")
                    state.setdefault("resultsLogged", {})[key] = now_iso
            if email_daily_report(live, state):
                daily[today_local] = now_iso
                state["dailyEmailed"] = {k: v for k, v in daily.items() if k >= today_local[:8]}

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
        "yesterday": [
            {"competition": r.get("competition"), "status": r.get("status"),
             "summary": r.get("summary"), "role": r.get("role")}
            for r in read_results_log_for(
                (datetime.now(TZ).date() - timedelta(days=1)).isoformat())
        ][:4],
    }
    HEALTH_PATH.write_text(json.dumps(health, indent=1), encoding="utf-8")
    state["lastRun"] = now_iso
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")

    print(f"checks done - {len(issues)} issue(s), {len(to_mail)} mailed, "
          f"{len(cleared)} resolved, window={'day' if within else 'night'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
