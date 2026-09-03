"""MiScore guest JSON API - fallback data source for the leaderboard poller.

Discovered 3 Sep 2026 by capturing the MiScore iPhone app's traffic after
leaderboard.miclub.com.au started returning "Organisation doesn't exist" for
the club while comps were running. The app reads a per-club host with
unauthenticated guest endpoints returning clean JSON:

    https://<club>.miclub.com.au/spring/guest/leaderboard/competitions/byDateRange
    https://<club>.miclub.com.au/spring/guest/leaderboard/<competitionId>

Data here has board-level scores only (no per-hole detail), so poller output
built from it carries empty holes[] - stories and hole notes degrade
gracefully. PDF results, NTP/LD etc. still come from the club site login.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date, timedelta

log = logging.getLogger("miscore.guestapi")

_UA = {"User-Agent": "Mozilla/5.0 (wwcc-kiosk poller)"}


def _get_json(url: str):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _host(club: str) -> str:
    return f"https://{club}.miclub.com.au"


def guest_list_competitions(club: str, days: int = 3) -> list[dict]:
    """Same shape as webscrape.list_competitions: [{leaderboardId, name, date}],
    newest first. Empty list on any failure."""
    try:
        frm = (date.today() - timedelta(days=max(0, days))).isoformat()
        to = date.today().isoformat()
        data = _get_json(f"{_host(club)}/spring/guest/leaderboard/competitions/"
                         f"byDateRange?fromDate={frm}&toDate={to}")
        comps = []
        for c in data.get("competitions") or []:
            name = str(c.get("name") or "").strip()
            gender = str(c.get("gender") or "").strip()
            if gender in ("Womens", "Mens", "Ladies", "Mixed") and \
                    gender.lower() not in name.lower():
                name = f"{name} {gender}"
            comps.append({
                "leaderboardId": str(c.get("competitionId")),
                "name": name,
                "date": str(c.get("date") or "")[:10] or None,
            })
        comps.sort(key=lambda c: c["date"] or "", reverse=True)
        return comps
    except Exception as exc:
        log.info("guest competitions fetch failed: %s", exc)
        return []


def _entry_name(entry: dict) -> str:
    names = []
    for p in entry.get("players") or []:
        nm = p.get("name") or {}
        full = f"{nm.get('first', '')} {nm.get('last', '')}".strip()
        if full:
            names.append(full)
    return " & ".join(names) or "Unknown"


def _num(v):
    try:
        return float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return None


def guest_board(club: str, board_id: str) -> dict | None:
    """Board data in the shapes poll() consumes.

    Returns {'field': [...], 'holeCount': int, 'par': int, 'format': str}
    where field rows match _board_players() output keys:
    {playerNo, player, hcp, homeClub, rank, boardThru, boardThruRaw, boardTotal}.
    None when the guest API can't serve the board.
    """
    try:
        data = _get_json(f"{_host(club)}/spring/guest/leaderboard/{board_id}")
    except Exception as exc:
        log.info("guest board fetch failed: %s", exc)
        return None
    entries = data.get("entries")
    if entries is None:
        return None

    hole_count, par_total = 0, 0
    field: list[dict] = []
    for e in entries:
        scs = e.get("competitionScorecards") or [{}]
        sc = scs[0]
        par_in, par_out = sc.get("parIn") or 0, sc.get("parOut") or 0
        if par_in and par_out:
            hole_count = max(hole_count, 18)
        elif par_in or par_out:
            hole_count = max(hole_count, 9)
        par_total = max(par_total, (par_in or 0) + (par_out or 0))

        thru_raw = str(e.get("thru") or "").strip()
        players = e.get("players") or [{}]
        hcp = _num(sc.get("handicap")) or _num(players[0].get("handicap"))
        rep = players[0].get("representing") or {}
        nett = _num(e.get("nett"))
        field.append({
            "playerNo": str(e.get("entrantId") or len(field) + 1),
            "player": _entry_name(e),
            "hcp": hcp if hcp is not None else 0.0,
            "homeClub": rep.get("homeClub") or "",
            "rank": _num(e.get("rank")) or len(field) + 1,
            "boardThru": int(thru_raw) if thru_raw.isdigit() else None,
            "boardThruRaw": thru_raw or None,
            # nett = stableford points (or net strokes for stroke comps)
            "boardTotal": nett,
        })

    fmt = str(data.get("format") or "").strip()
    name = str(data.get("name") or "")
    if not hole_count:
        hole_count = 9 if "9 hole" in name.lower() else 18
    return {"field": field, "holeCount": hole_count, "par": par_total, "format": fmt}
