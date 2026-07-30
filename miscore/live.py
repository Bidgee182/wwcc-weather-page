"""Live leaderboard poller for a TV kiosk dashboard.

Polls the public MiClub board for a club's *current* competition and emits a
single kiosk-ready JSON every `--interval` seconds, with everything a live
scrolling leaderboard + "on a heater" + "coming last" panels need:

  - each player's live points (summed from their scorecard, NOT the board's
    "Total" column, which stays "-" until a card is officially confirmed)
  - `thru` (holes actually played), birdies, and their last few holes
  - derived views: leaders, heaters (just made a birdie / on a run), coming last
  - `events`: what changed since the previous poll (new birdies, eagles)

No login, no key, no app - plain public HTTP, so it runs anywhere.

  python -m miscore.live --once                 # one poll -> out/live-leaderboard.json
  python -m miscore.live                         # poll every 60s forever
  python -m miscore.live --serve 8787            # + serve the JSON at http://<pc>:8787/ (CORS open)
  python -m miscore.live --comp "Navigate 9"     # pick a comp by name (default: newest board today)

The kiosk reads either out/live-leaderboard.json or GET http://<pc>:8787/.
"""

import argparse
import html
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import sleep

from .webscrape import BASE, _get, _parse_holes, list_competitions

log = logging.getLogger("miscore.live")

HOLE_MAP: dict[int, int] = {}
HOLE_COUNT_OVERRIDE: int | None = None

# ---------------------------------------------------------------------------
# board + scorecard parsing (live-aware; the shared webscrape helpers assume
# completed rounds and read the board's "Total", which is useless mid-round)
# ---------------------------------------------------------------------------


def _board_players(page: str) -> list[dict]:
    """Field for a board: [{playerNo, player, hcp, homeClub, rank}] in board order."""
    out: list[dict] = []
    for m in re.finditer(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        row = m.group(1)
        link = re.search(r'scorecard\?[^"]*player=(\d+)"[^>]*>([^<]+)<', row)
        if not link:
            continue
        player_no = link.group(1)
        # Find ALL names linked to this player number (4BBB has 2 players, same scorecard)
        all_names = re.findall(
            r'scorecard\?[^"]*player=' + re.escape(player_no) + r'"[^>]*>([^<]+)<', row
        )
        all_names = list(dict.fromkeys(html.unescape(n).strip() for n in all_names if n.strip()))
        # All handicaps in row in order; 4BBB has one per player
        hcp_ms = re.findall(r"\[(\+?\d+(?:\.\d+)?)\]", row)
        hcps = [float(v.replace("+", "")) for v in hcp_ms]
        if len(all_names) > 1:
            # Team: embed each player's hcp next to their name, clear hcp field (no grades)
            def _fh(v): return str(int(v)) if v % 1 == 0 else f'{v:.1f}'
            parts = [n + (f' [{_fh(hcps[i])}]' if i < len(hcps) else '') for i, n in enumerate(all_names)]
            name_txt = ' & '.join(parts)
            hcp = None
        else:
            name_txt = all_names[0] if all_names else html.unescape(link.group(2)).strip()
            hcp = hcps[0] if hcps else None
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)]
        cells = [c for c in cells if c is not None]
        rank = None
        for c in cells:
            if re.fullmatch(r"\d+", c.strip()):
                rank = int(c.strip())
                break
        home = ""
        first_name = all_names[0] if all_names else name_txt
        for i, c in enumerate(cells):
            if first_name in c and i + 1 < len(cells):
                home = cells[i + 1]
                break
        out.append(
            {
                "playerNo": player_no,
                "player": name_txt,
                "hcp": hcp,
                "homeClub": home,
                "rank": rank,
            }
        )
    return out


def _played(holes: list[dict]) -> list[dict]:
    # Include holes where either strokes OR points is recorded.
    # MiClub sometimes uploads stableford points before raw strokes (app sync lag),
    # so requiring strokes alone misses those holes and undercounts the score.
    return [h for h in holes if isinstance(h.get("strokes"), int) or isinstance(h.get("points"), int)]


def _thru(holes: list[dict]) -> int:
    # Use the last hole index that has ANY data, not just len(played).
    # Stableford pickups (no return) show as strokes=None, points=None in MiClub
    # but the player DID play the hole. If later holes have data, the blank must
    # be a pickup, so count up to the last hole with any real data.
    last = -1
    for i, h in enumerate(holes):
        if isinstance(h.get("strokes"), int) or isinstance(h.get("points"), int):
            last = i
    return last + 1 if last >= 0 else 0


def _course_shape(page: str) -> tuple[int, int]:
    """True (holeCount, par) for the course from a scorecard page."""
    holes = par = 0
    seen_pars: set[int] = set()  # 4BBB scorecards have 2 player sections - deduplicate
    for m in re.finditer(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>", m.group(1))]
        cells = [c for c in cells if c]
        if not cells or cells[0].lower() != "par":
            continue
        nums = [int(c) for c in cells[1:] if re.fullmatch(r"\d+", c)]
        if not nums:
            continue
        subtotal = nums[-1]
        if subtotal in seen_pars:
            continue
        seen_pars.add(subtotal)
        par += subtotal
        holes += 18 if subtotal >= 60 else 9
    return holes, par


def _hole_note(h: dict) -> str | None:
    """Human label for a notable hole (eagle/birdie)."""
    par, st = h.get("par"), h.get("strokes")
    if not par or not isinstance(st, int):
        return None
    if st <= par - 2:
        return f"eagle on {h['hole']}"
    if st == par - 1:
        return f"birdie on {h['hole']}"
    return None


# ---------------------------------------------------------------------------
# polling
# ---------------------------------------------------------------------------


_AEST = timezone(timedelta(hours=10))

def find_board(club: str, comp: str | None, days: int) -> dict | None:
    """Newest board today (or newest matching `comp`)."""
    comps = list_competitions(club, days)
    if not comps:
        return None
    if comp:
        comps = [c for c in comps if comp.lower() in c["name"].lower()]
    today = datetime.now(_AEST).date().isoformat()  # AEST date - runner is UTC
    todays = [c for c in comps if c.get("date") == today]
    pool = todays or comps
    return pool[0] if pool else None


def _fetch_card(club: str, board_id: str, pno: str) -> str:
    """Raw scorecard HTML for one player (retried once). '' on failure."""
    for attempt in range(2):
        try:
            return _get(f"{BASE}/scorecard?club={club}&leaderboardId={board_id}&player={pno}")
        except Exception as e:  # noqa: BLE001
            if attempt:
                log.warning("scorecard %s failed: %s", pno, e)
                return ""
            sleep(0.3)
    return ""


def poll(club: str, board: dict, workers: int, prev: dict[str, dict]) -> dict:
    """One full read of a board -> kiosk JSON."""
    board_id = board["leaderboardId"]
    page = _get(f"{BASE}/display-leaderboard?club={club}&leaderboardId={board_id}")
    type_m = re.search(r"-\s*(Stableford|Stroke|Par|Gross)\b", page)
    comp_type = (type_m.group(1) if type_m else "").strip()
    field_ = _board_players(page)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages = list(ex.map(lambda p: _fetch_card(club, board_id, p["playerNo"]), field_))

    hole_count = 0
    par_total = 0
    players: list[dict] = []
    events: list[dict] = []
    for base_, page_c in zip(field_, pages):
        holes = _parse_holes(page_c) if page_c else []
        if HOLE_MAP:
            holes = [{**h, "hole": HOLE_MAP.get(h["hole"], h["hole"])} for h in holes]
        shape_holes, shape_par = _course_shape(page_c) if page_c else (0, 0)
        hole_count = max(hole_count, shape_holes)
        par_total = max(par_total, shape_par)
        played = _played(holes)
        thru = _thru(holes)
        points = sum(h.get("points") or 0 for h in played)
        birdies = sum(1 for h in played if h.get("par") and isinstance(h.get("strokes"), int) and h["strokes"] < h["par"])
        last = played[-3:]
        p = {
            **base_,
            "thru": thru,
            "points": points,
            "birdies": birdies,
            "holes": holes,
            "last": [{"hole": h["hole"], "par": h.get("par"), "strokes": h.get("strokes"), "strokes2": h.get("strokes2"), "points": h.get("points")} for h in last],
        }
        players.append(p)

        name = base_["player"]
        was = prev.get(name)
        if was and thru > was["thru"]:
            for h in played[was["thru"]:]:
                note = _hole_note(h)
                if note:
                    events.append({"player": name, "note": note, "hole": h["hole"]})
        prev[name] = {"thru": thru, "points": points, "birdies": birdies}

    course_holes = hole_count  # actual holes from scorecard (before finished-threshold override)
    if HOLE_COUNT_OVERRIDE is not None:
        hole_count = HOLE_COUNT_OVERRIDE

    ranked = sorted(players, key=lambda p: (-p["points"], -p["thru"], p["player"]))
    for i, p in enumerate(ranked, 1):
        p["liveRank"] = i

    def on_heater(p: dict) -> tuple[str | None, int | None]:
        """Returns (note, scorerIdx) or (None, None). scorerIdx: 0=first, 1=second, 2=both."""
        if p["thru"] == 0:
            return None, None
        last = p["last"]
        if not last:
            return None, None
        last_hole = last[-1]
        pts = last_hole.get("points") or 0
        # Teams (hcp=None, 4BBB) show 3+ pts; individuals require 4+ (eagle)
        threshold = 3 if p.get("hcp") is None else 4
        if pts < threshold:
            return None, None
        note = f"{pts} pts on hole {last_hole['hole']}"
        # For team events, determine which player scored using stroke comparison
        scorer_idx = None
        if p.get("hcp") is None:
            par = last_hole.get("par")
            s1  = last_hole.get("strokes")
            s2  = last_hole.get("strokes2")
            if par is not None:
                if s1 is not None and s2 is not None:
                    d1, d2 = s1 - par, s2 - par
                    if d1 == d2:
                        scorer_idx = 2  # both players scored equally
                    else:
                        scorer_idx = 0 if d1 < d2 else 1
                elif s1 is not None:
                    scorer_idx = 0 if max(0, 2 - (s1 - par)) >= pts else 1
                elif s2 is not None:
                    scorer_idx = 1 if max(0, 2 - (s2 - par)) >= pts else 0
        return note, scorer_idx

    heaters = []
    for p in ranked:
        note, scorer_idx = on_heater(p)
        if note:
            heaters.append({"player": p["player"], "note": note, "points": p["points"], "thru": p["thru"], "scorerIdx": scorer_idx})

    coming_last = sorted(
        [p for p in players if p["thru"] >= 12],
        key=lambda p: (p["points"], -p["thru"], p["player"]),
    )

    now = datetime.now(timezone.utc)
    return {
        "competition": board["name"],
        "type": comp_type,
        "date": board.get("date"),
        "leaderboardId": board_id,
        "holeCount": hole_count or None,
        "courseHoles": course_holes or hole_count or None,
        "par": par_total or None,
        "generatedAt": now.isoformat(),
        "playerCount": len(players),
        "started": any(p["thru"] > 0 for p in players),
        "players": ranked,
        "leaders": ranked[:10],
        "heaters": heaters[:8],
        "comingLast": [
            {"player": p["player"], "hcp": p["hcp"], "points": p["points"], "thru": p["thru"]}
            for p in coming_last[:10]
        ],
        "events": events,
    }


# ---------------------------------------------------------------------------
# tiny CORS server (optional)
# ---------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.blob: dict = {"status": "starting"}

    def set(self, blob: dict) -> None:
        with self.lock:
            self.blob = blob

    def get(self) -> dict:
        with self.lock:
            return self.blob


def _serve(port: int, state: _State) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(state.get()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("serving live JSON at http://0.0.0.0:%d/ (CORS open)", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live MiClub leaderboard poller for a kiosk")
    ap.add_argument("--club", default="wwcc")
    ap.add_argument("--comp", default=None, help="pick a comp by name substring (default: newest board today)")
    ap.add_argument("--days", type=int, default=3, help="how far back to look for the board")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls")
    ap.add_argument("--workers", type=int, default=12, help="concurrent scorecard fetches")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT", help="also serve the JSON on this port (CORS open)")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent.parent / "out" / "live-leaderboard.json")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    args.out.parent.mkdir(parents=True, exist_ok=True)

    state = _State()
    if args.serve:
        _serve(args.serve, state)

    prev: dict[str, dict] = {}
    while True:
        try:
            board = find_board(args.club, args.comp, args.days)
            if not board:
                blob = {
                    "status": "no board",
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "note": f"no competition for {args.club} in the last {args.days} days",
                }
                log.info("no board found")
            else:
                blob = poll(args.club, board, args.workers, prev)
                lead = blob["leaders"][0] if blob["leaders"] else None
                log.info(
                    "%s: %d players, %s, leader %s",
                    blob["competition"], blob["playerCount"],
                    "started" if blob["started"] else "not started",
                    f'{lead["player"]} {lead["points"]}pts' if lead else "-",
                )
        except Exception as e:  # noqa: BLE001
            log.warning("poll failed: %s", e)
            blob = {"status": "error", "error": str(e), "generatedAt": datetime.now(timezone.utc).isoformat()}

        state.set(blob)
        args.out.write_text(json.dumps(blob, indent=1), encoding="utf-8")

        if args.once:
            return 0
        sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
