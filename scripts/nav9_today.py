#!/usr/bin/env python3
"""Build data/nav9_today.json for the TV-kiosk Navigate 9s panel.

Scrapes TODAY's "Navigate 9" MiClub board (reusing this repo's miscore scraper),
sums each completed card to a gross, then applies data/nav9_handicaps.json (the
weekly name/team/handicap lookup pulled from the Navigate 9s app) to show each
player's nett. Navigate 9s is a 9-hole stroke comp: nett = gross - that week's
handicap. The Navigate 9s app stays the source of truth for official results;
this is the live kiosk view.
"""
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from miscore.live import (  # noqa: E402
    BASE,
    _board_players,
    _fetch_card,
    _get,
    _parse_holes,
    find_board,
)

CLUB = "wwcc"
COMP = "Navigate 9"
HOLES = 9
HANDI = os.path.join(ROOT, "data", "nav9_handicaps.json")
OUT = os.path.join(ROOT, "data", "nav9_today.json")


def norm_key(name: str) -> str:
    """Order-independent match key - must match nav9_build_handicaps.py exactly."""
    return " ".join(sorted(re.findall(r"[a-z0-9]+", (name or "").lower())))


def gross_of(holes: list[dict]) -> tuple[int, int]:
    """(gross, holes played). Only holes with real strokes count."""
    s = [h["strokes"] for h in holes if isinstance(h.get("strokes"), int)]
    return sum(s), len(s)


def main() -> int:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=11))).date().isoformat()  # Sydney AEDT

    try:
        lookup = json.load(open(HANDI, encoding="utf-8"))
    except Exception as e:
        print(f"no handicap lookup: {e}", file=sys.stderr)
        lookup = {"week": None, "season": None, "players": []}
    by_key = {p["key"]: p for p in lookup.get("players", [])}

    rounds, in_progress, unmatched = [], 0, []
    board = find_board(CLUB, COMP, days=2)
    board_found = bool(board)
    if board:
        page = _get(f"{BASE}/display-leaderboard?club={CLUB}&leaderboardId={board['leaderboardId']}")
        field = _board_players(page) if page else []
        for pl in field:
            name = pl.get("player") or ""
            if not name or " & " in name or name.startswith("Hole "):
                continue  # skip team/field rows
            try:
                card = _fetch_card(CLUB, board["leaderboardId"], pl.get("playerNo"))
            except Exception:
                card = ""
            gross, played = gross_of(_parse_holes(card) if card else [])
            if played < HOLES:
                in_progress += 1
                continue  # card not finished yet - no nett
            m = by_key.get(norm_key(name))
            if not m:
                unmatched.append(name)
                continue
            rounds.append({
                "name": m["name"],
                "team": m["team"],
                "gross": gross,
                "handicap": m["handicap"],
                "nett": gross - m["handicap"],
            })
    rounds.sort(key=lambda r: (r["nett"], r["gross"], r["name"]))

    out = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": today,
        "season": lookup.get("season"),
        "week": lookup.get("week"),
        "boardFound": board_found,
        "count": len(rounds),
        "inProgress": in_progress,
        "unmatched": unmatched[:20],
        "rounds": rounds,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"WROTE {OUT}: board={board_found} completed={len(rounds)} "
          f"inProgress={in_progress} unmatched={len(unmatched)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
