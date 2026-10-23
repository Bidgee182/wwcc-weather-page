#!/usr/bin/env python3
"""Build data/nav9_handicaps.json for the TV-kiosk Navigate 9s board.

Reads the Navigate 9s roster from that app's Supabase and writes a small lookup -
each player's team and the handicap they play off THIS competition week - that
the kiosk applies to the scraped MiClub gross to show netts. The file lives in
this (leaderboard) repo; the Navigate 9s app is never touched.

Week 1 of a season plays off the starting handicap, which is stored on the
roster, so this runs engine-free. From Week 2 the weekly handicap is the Nav 9
engine's rolling average (not a stored number); pass --week-handicaps <file.json>
(a {player_id: handicap} map exported from the app) to use those instead.

Env (read-only):
  NAV9_SUPABASE_URL, NAV9_SUPABASE_KEY   (the Navigate 9s project's service key)
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "nav9_handicaps.json")


def norm_key(name: str) -> str:
    """Order-independent match key: lowercase alnum tokens, sorted.

    Matches "Smith, John" to "John Smith" regardless of how MiClub prints it."""
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return " ".join(sorted(toks))


def rest(url: str, key: str, path: str):
    req = urllib.request.Request(
        f"{url}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def days_between(to_ymd: str, from_ymd: str) -> int:
    a = dt.date.fromisoformat(to_ymd)
    b = dt.date.fromisoformat(from_ymd)
    return (a - b).days


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-handicaps", help="JSON {player_id: handicap} for week 2+ (from the Nav 9 engine)")
    ap.add_argument("--season", default=None, help="season name (default: the active one)")
    args = ap.parse_args(argv)

    url = os.environ.get("NAV9_SUPABASE_URL")
    key = os.environ.get("NAV9_SUPABASE_KEY")
    if not url or not key:
        print("NAV9_SUPABASE_URL / NAV9_SUPABASE_KEY not set", file=sys.stderr)
        return 2

    seasons = rest(url, key, "seasons?select=id,name,status,starts_on,config&order=starts_on.desc")
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=11))).date().isoformat()  # Sydney AEDT
    if args.season:
        season = next((s for s in seasons if s["name"] == args.season), None)
    else:
        season = next((s for s in seasons if s.get("status") == "active"), None) \
            or next((s for s in seasons if (s.get("starts_on") or "9999") <= today), None) \
            or seasons[0]
    if not season:
        print("no season found", file=sys.stderr)
        return 2
    cfg = season.get("config") or {}
    weeks = int(cfg.get("weeks", 16))
    split = int(cfg.get("splitAfterWeek", 8))
    ws = cfg.get("weekStart")
    wsa = cfg.get("weekStartAfterSplit")
    par_front, par_back = 36, 35

    # current competition week from the date
    week = 1
    if ws and days_between(today, ws) >= 0:
        week = min(days_between(today, ws) // 7 + 1, weeks)
        if wsa and week > split:
            d2 = days_between(today, wsa)
            week = min(split + d2 // 7 + 1, weeks) if d2 >= 0 else split + 1
    week = max(week, 1)

    sid = season["id"]
    members = rest(url, key, f"team_members?season_id=eq.{sid}"
                   "&select=player_id,team_id,starting_handicap,is_sub,non_playing,joined_week,left_after_week&limit=2000")
    teams = {t["id"]: t["name"] for t in rest(url, key, f"teams?season_id=eq.{sid}&select=id,name&limit=2000")}
    pids = list({m["player_id"] for m in members})
    names = {}
    for i in range(0, len(pids), 300):
        chunk = ",".join(pids[i:i + 300])
        for p in rest(url, key, f"players?id=in.({chunk})&select=id,full_name"):
            names[p["id"]] = p["full_name"]

    wk_override = {}
    if args.week_handicaps:
        wk_override = {str(k): v for k, v in json.load(open(args.week_handicaps, encoding="utf-8")).items()}

    players = []
    for m in members:
        if m.get("non_playing"):
            continue
        jf, lt = m.get("joined_week"), m.get("left_after_week")
        if (jf and week < jf) or (lt is not None and week > lt):
            continue  # not on the team this week
        pid = m["player_id"]
        hcp = wk_override.get(pid, m["starting_handicap"]) if (week > 1 and wk_override) else m["starting_handicap"]
        nm = names.get(pid, "?")
        players.append({
            "name": nm,
            "team": teams.get(m["team_id"], "?"),
            "handicap": hcp,
            "key": norm_key(nm),
        })
    players.sort(key=lambda p: p["name"])

    basis = "starting handicaps (week 1)" if week == 1 or not wk_override else "engine weekly handicaps"
    out = {
        "season": season["name"],
        "week": week,
        "parFront": par_front,
        "parBack": par_back,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "basis": basis,
        "count": len(players),
        "players": players,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"WROTE {OUT}: season {season['name']} week {week}, {len(players)} players ({basis})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
