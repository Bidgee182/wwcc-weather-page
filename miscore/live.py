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
    """Field for a board: [{playerNo, player, hcp, homeClub, rank, boardThru}] in board order."""
    # Pre-pass: find the "Thru" column index from the <th> header row so we can extract
    # the board's own Thru count per player (used to catch blank-NR scorecard gaps).
    thru_col: int | None = None
    for m in re.finditer(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        ths = re.findall(r"(?s)<th[^>]*>(.*?)</th>", m.group(1))
        if not ths:
            continue
        labels = [html.unescape(re.sub(r"<[^>]+>", " ", t)).strip().lower() for t in ths]
        for i, lbl in enumerate(labels):
            if "thru" in lbl:
                thru_col = i
                break
        if thru_col is not None:
            break

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
        tds_raw = re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in tds_raw]
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
        # Extract board's Thru value from the detected column
        board_thru: int | None = None
        if thru_col is not None and thru_col < len(tds_raw):
            cell_txt = html.unescape(re.sub(r"<[^>]+>", " ", tds_raw[thru_col])).strip()
            if re.fullmatch(r"\d+", cell_txt):
                board_thru = int(cell_txt)
        out.append(
            {
                "playerNo": player_no,
                "player": name_txt,
                "hcp": hcp,
                "homeClub": home,
                "rank": rank,
                "boardThru": board_thru,
            }
        )
    return out


def _played(holes: list[dict]) -> list[dict]:
    # Use "played" flag set by the parser (True for any entered cell, including
    # explicit "-" pickups which have no int value). Fall back to int checks for
    # data parsed before the "played" field was introduced.
    return [h for h in holes
            if h.get("played") or isinstance(h.get("strokes"), int) or isinstance(h.get("points"), int)]


def _thru(holes: list[dict], hole_count: int = 0) -> int:
    # Count all holes explicitly entered on the card - including pickups ("-").
    # Never use hole index: shotgun starts mean the index order is not play order.
    played_nums = {h["hole"] for h in holes
                   if h.get("played") or isinstance(h.get("strokes"), int) or isinstance(h.get("points"), int)}
    played_count = len(played_nums)
    if hole_count > 0 and 0 < played_count < hole_count:
        # Detect blank NR holes sandwiched between played holes.
        # A blank hole between min_played and max_played can only be an NR (the
        # scorer left both strokes and score empty); a genuinely unplayed hole is
        # always at the tail end of the played range.
        all_nums = {h["hole"] for h in holes}
        min_p, max_p = min(played_nums), max(played_nums)
        sandwiched = sum(1 for n in all_nums if min_p < n < max_p and n not in played_nums)
        if played_count + sandwiched >= hole_count:
            return hole_count
    return played_count


def _course_shape(page: str) -> tuple[int, int]:
    """True (holeCount, par) for the course from a scorecard page."""
    holes = par = 0
    # Dedup by the full par-row tuple (not just subtotal) so front/back nines with the same
    # subtotal (e.g. Par 36 + Par 36 = Par 72) are kept distinct, while 4BBB repeated
    # sections (identical rows) are still collapsed to one.
    seen_rows: set[tuple] = set()
    for m in re.finditer(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>", m.group(1))]
        cells = [c for c in cells if c]
        if not cells or cells[0].lower() != "par":
            continue
        nums = [int(c) for c in cells[1:] if re.fullmatch(r"\d+", c)]
        if not nums:
            continue
        key = tuple(nums)
        if key in seen_rows:
            continue
        seen_rows.add(key)
        subtotal = nums[-1]
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


def _story(played: list[dict], is_stableford: bool = True, player: str = "") -> dict | None:
    """Generate a Scoreboard Story from a player's played holes (hole-number order).

    is_stableford: use points-based language (3-pointer, 4-pointer etc).
                   False = stroke play language (birdie, eagle).
    """
    n = len(played)
    if n < 2:
        return None

    def gpts(h):      return h.get("points")
    def is_wipe(h):   return gpts(h) is None or gpts(h) == 0
    def is_bogey(h):  return gpts(h) == 1
    def is_par(h):    return gpts(h) == 2
    def is_birdie(h): return gpts(h) is not None and gpts(h) >= 3
    def is_eagle(h):  return gpts(h) is not None and gpts(h) >= 4

    def hnum(h):      return h.get("hole", "?")

    def holes_str(hs):
        nums = [hnum(h) for h in hs]
        if len(nums) == 1: return f"hole {nums[0]}"
        if len(nums) == 2: return f"holes {nums[0]} & {nums[1]}"
        if len(nums) == 3: return f"holes {nums[0]}, {nums[1]} & {nums[2]}"
        return f"holes {nums[0]}-{nums[-1]}"

    def _max_run(pred):
        """Longest consecutive run of holes matching pred anywhere in played."""
        best = cur = 0
        for h in played:
            if pred(h): cur += 1; best = max(best, cur)
            else: cur = 0
        return best

    def birdie_word(h=None):
        if is_stableford:
            pts = gpts(h) if h else 3
            return f"{pts}-pointer"
        return "birdie"

    def eagle_word(h=None):
        if is_stableford:
            pts = gpts(h) if h else 4
            return f"{pts}-pointer"
        return "eagle"

    def birdies_word():  return "3-pointers" if is_stableford else "birdies"
    def bogey_word():    return "1-pointer"  if is_stableford else "bogey"
    def bogeys_word():   return "1-pointers" if is_stableford else "bogeys"

    def story(title, detail, tier, emoji):
        return {"title": title, "detail": detail, "tier": tier, "emoji": emoji}

    def _var(*opts: str) -> str:
        return opts[abs(hash(player)) % len(opts)]

    # ── Per-hole priority stories ────────────────────────────────────────────

    # Hole in one
    for h in played:
        if h.get("strokes") == 1 and h.get("par") == 3:
            return story("ACE!", f"Hole in one on hole {hnum(h)} - buy them a drink!", "gold", "🎯")

    # Two or more eagles in the round
    eagle_holes = [h for h in played if is_eagle(h)]
    if len(eagle_holes) >= 2:
        detail = (f"{len(eagle_holes)} big ones - {holes_str(eagle_holes)}" if is_stableford
                  else f"{len(eagle_holes)} eagles on {holes_str(eagle_holes)} - this isn't fair")
        return story("Someone Call Security", detail, "gold", "🚔")

    # Consecutive birdie streak from most recent hole backward
    birdie_streak = 0
    for h in reversed(played):
        if is_birdie(h): birdie_streak += 1
        else: break
    streak_holes = played[-birdie_streak:] if birdie_streak else []

    if birdie_streak >= 3:
        return story("Running Hot", f"{birdie_streak} straight {birdies_word()} on {holes_str(streak_holes)}", "gold", "🔥")
    if birdie_streak >= 2:
        return story("On the Charge", f"Back-to-back {birdies_word()} on {holes_str(streak_holes)}", "orange", "⚡")

    # Eagle on most recent hole
    if is_eagle(played[-1]):
        last_h = played[-1]
        if n >= 3 and is_wipe(played[-2]) and is_wipe(played[-3]):
            return story("Out of Nowhere",
                         f"Two wipes then a {eagle_word(last_h)} on {holes_str([played[-3], played[-2], last_h])}",
                         "gold", "🎭")
        return story("The Big Gun", f"{eagle_word(last_h)} on hole {hnum(last_h)}", "gold", "💥")

    # Wipe streak
    wipe_streak = 0
    for h in reversed(played):
        if is_wipe(h): wipe_streak += 1
        else: break
    wipe_holes = played[-wipe_streak:] if wipe_streak else []
    if wipe_streak >= 4:
        return story("Already at the Bar", f"{wipe_streak} wipes in a row on {holes_str(wipe_holes)}", "red", "🍺")
    if wipe_streak == 3:
        return story("Left the Clubs at Home", f"3 wipes in a row on {holes_str(wipe_holes)}", "red", "🏠")
    if wipe_streak == 2 and n >= 5:
        return story("Rough Patch", f"2 wipes on {holes_str(wipe_holes)} - club still in the bag", "red", "🩹")

    # Bogey train (escalating shade)
    bogey_streak = 0
    for h in reversed(played):
        if is_bogey(h): bogey_streak += 1
        else: break
    bogey_holes = played[-bogey_streak:] if bogey_streak else []
    if bogey_streak >= 6:
        return story("Someone Check On Them", f"{bogey_streak} {bogeys_word()} in a row on {holes_str(bogey_holes)} - {_var('Cronulla Sharks finals form', 'playing like they lost the rulebook', 'pure Sunday arvo golf energy')}", "red", "🚑")
    if bogey_streak >= 5:
        return story("Is This Fun Anymore?", f"{bogey_streak} {bogeys_word()} on the bounce on {holes_str(bogey_holes)} - {_var('pure St Kilda fan energy', 'cricket pitch level patience required', 'V8s at Bathurst: going nowhere fast')}", "red", "😭")
    if bogey_streak >= 4:
        return story("Still Grinding...", f"{bogey_streak} straight {bogeys_word()} on {holes_str(bogey_holes)}", "blue", "😤")
    if bogey_streak >= 3:
        return story("The Grind", f"{bogey_streak} {bogeys_word()} in a row on {holes_str(bogey_holes)}", "blue", "⛏️")

    # Two-hole transitions
    if n >= 2:
        prev_h, last_h = played[-2], played[-1]
        two = holes_str([prev_h, last_h])
        if is_eagle(prev_h) and is_wipe(last_h):
            return story("The Rollercoaster", f"{eagle_word(prev_h)} then a wipe on {two} - {_var('Adelaide Crows energy: brilliant then baffling', 'cricket in one over: six then a wicket', 'peak Australian sport')}", "orange", "🎢")
        if is_birdie(prev_h) and is_wipe(last_h):
            return story("Hero to Zero", f"{birdie_word(prev_h)} then a wipe on {two} - {_var('NZ Warriors in one set', 'feast then famine', 'hot start, cold finish')}", "orange", "📉")
        if gpts(prev_h) == 3 and is_bogey(last_h):
            detail = (f"3-pointer straight into a {bogey_word()} on {two}" if is_stableford
                      else f"Birdie straight into a bogey on {two}")
            return story("Giveth and Taketh Away", detail, "orange", "🔄")
        if is_wipe(prev_h) and is_birdie(last_h):
            return story("The Bounce Back", f"Wipe then a {birdie_word(last_h)} on {two}", "orange", "↩️")

    # Par streak
    par_streak = 0
    for h in reversed(played):
        if is_par(h): par_streak += 1
        else: break
    par_holes = played[-par_streak:] if par_streak else []
    if par_streak >= 9:
        return story("The Metronome", f"{par_streak} pars on {holes_str(par_holes)}. Just. Pars.", "blue", "⏱️")
    if par_streak >= 7:
        return story("Human Highway", f"{par_streak} pars in a row on {holes_str(par_holes)} - accountant energy", "blue", "🛣️")
    if par_streak >= 5:
        return story("Vanilla Golf", f"{par_streak} straight pars on {holes_str(par_holes)}", "blue", "🍦")
    if par_streak >= 3:
        return story("Finding a Rhythm", f"{par_streak} pars on the bounce on {holes_str(par_holes)}", "blue", "🎵")

    # Bad start: wipes from the very first hole
    wipes_from_start = 0
    for h in played:
        if is_wipe(h): wipes_from_start += 1
        else: break
    start_holes = played[:wipes_from_start]
    if wipes_from_start >= 7:
        return story("Save It For Next Week", f"Still searching - {wipes_from_start} wipes to open on {holes_str(start_holes)}", "red", "📅")
    if wipes_from_start >= 5:
        return story("Looking for the Course", f"Nothing from the first {wipes_from_start} on {holes_str(start_holes)}", "red", "🗺️")
    if wipes_from_start >= 3 and n <= 9:
        return story("Slow Starter", f"Nil from the first {wipes_from_start} on {holes_str(start_holes)}", "blue", "🐢")

    # ── Whole-round aggregate stories (fallback when no per-hole story fires) ─
    if n < 6:
        return None

    total_wipes   = sum(1 for h in played if is_wipe(h))
    total_bogeys  = sum(1 for h in played if is_bogey(h))
    total_pars    = sum(1 for h in played if is_par(h))
    total_birdies = sum(1 for h in played if is_birdie(h))
    total_pts     = sum(gpts(h) or 0 for h in played)

    # Wipe-heavy rounds
    if total_wipes >= 8:
        _v = _var("more leakage than the Essendon defence", "more gaps than a country fence", "the course is just helping itself")
        return story("The Colander", f"{total_wipes} wipes from {n} holes - {_v}", "red", "🪣")
    if total_wipes >= 6:
        return story("Points-Free Diet", f"{total_wipes} wipes - {_var('scoring as often as North Melbourne make the finals', 'the fairways are winning today', 'the course is undefeated today')}", "red", "🌵")
    if total_wipes >= 4 and n >= 9:
        return story("The Streaker (Not That Kind)", f"{total_wipes} wipes in {n} holes - {_var('Gold Coast Titans form right here', 'tough day at the office', 'the bogey count approves')}", "red", "😵")

    # Bogey storms (longest run anywhere, not just current tail)
    longest_bogey_run = _max_run(is_bogey)
    if longest_bogey_run >= 5:
        return story("Kick, Chase, Repeat",
                     f"{longest_bogey_run} {bogeys_word()} in a row at some point - {_var('like Richmond in October: close but never converting', 'always knocking, never scoring', 'par country but the pars are in hiding')}",
                     "red", "🦶")
    if total_bogeys >= 9:
        return story("The 1-Pointer Specialist",
                     f"{total_bogeys} {bogeys_word()} on the card - {_var('consistent as a Parramatta finals meltdown', 'the bogey machine is fully operational', 'nine holes, nine lessons')}",
                     "blue", "🔩")
    if total_bogeys >= 7 and total_wipes == 0:
        return story("Nothing But Bogeys",
                     f"{total_bogeys} {bogeys_word()}, zero wipes - {_var('hanging in like a Bulldogs fan in July', 'zero wipes is something to hang your hat on', 'grit without the glory')}",
                     "blue", "⚙️")

    # Total birdies haul (not bunched - streaks already caught above)
    bw = "3-pointers+" if is_stableford else "birdies"
    if total_birdies >= 5:
        return story("The Merchant", f"{total_birdies} {bw} on the card - {_var('Brisbane Lions style, finding the scoreboard from everywhere', 'like Warnie: never stops attacking', 'all over the card in the best possible way')}", "orange", "🛍️")
    if total_birdies >= 3 and n >= 12:
        return story("Spot Fires", f"{total_birdies} {bw} scattered through the round - {_var('Souths Rabbitohs attack: unpredictable but it keeps coming', 'keeps finding ways to score', 'scattered but effective')}", "orange", "✨")

    # Searching - wipes with no birdies
    if total_wipes >= 3 and total_birdies == 0 and n >= 9:
        _v = _var("West Coast Eagles vibes: all effort, no score", "the flagstick is definitely moving", "hard work, light reward")
        return story("Still Searching",
                     f"{total_wipes} wipes, no birdies from {n} holes - {_v}",
                     "red", "🔍")

    # Clean card - no wipes
    if total_wipes == 0 and n >= 12:
        return story("Squeaky Clean",
                     f"Zero wipes from {n} holes - {_var('cleaner than a Penrith defensive set', 'not a wipe in sight - incredible', 'faultless card so far')}",
                     "blue", "🧹")

    # Scoring average
    if n >= 9:
        avg = total_pts / n
        if avg >= 2.8:
            return story("That's a Good Card",
                         f"Averaging {avg:.1f}pts per hole through {n} - {_var('Geelong Cats efficiency: boring, effective, winning', 'cricket scoring: controlled, methodical, relentless', 'V8 lap pace: consistent all day')}",
                         "gold", "📈")
        if avg >= 2.4:
            return story("On the Right Track",
                         f"Averaging {avg:.1f}pts per hole through {n} - {_var('Newcastle Knights vibes: steady, building, watch this space', 'not flashy but solid', 'the engine is warm')}",
                         "orange", "🛤️")
        if avg < 0.8:
            _v = _var("tougher than a Wests Tigers fan meeting", "the course is not cooperating", "character-building stuff")
            return story("Lost in the Rough",
                         f"Under a point a hole through {n} - {_v}",
                         "red", "🌿")

    # All four outcomes seen (full experience)
    if total_wipes > 0 and total_bogeys > 0 and total_pars > 0 and total_birdies > 0:
        return story("The Complete Package",
                     f"Wipes, bogeys, pars AND birdies - {_var('the full Manly Sea Eagles package: drama, flair, and something for everyone', 'a round that covers all four food groups', 'the full Australian golf experience')}",
                     "blue", "🎰")

    # Pure bogeys only - no other outcomes
    if total_bogeys >= 5 and total_wipes == 0 and total_birdies == 0 and n >= 9:
        return story("The Consistent Battler",
                     f"{total_bogeys} {bogeys_word()} and nothing else - {_var('steady as a Canberra Raiders defensive set', 'exactly what it says on the tin', 'no surprises, no drama')}",
                     "blue", "🔩")

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
    is_stableford = comp_type.lower() not in ("stroke", "gross")
    field_ = _board_players(page)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages = list(ex.map(lambda p: _fetch_card(club, board_id, p["playerNo"]), field_))

    # Pre-pass: establish course shape before the player loop so _thru() has the
    # correct hole_count for sandwiched-NR detection.
    hole_count = 0
    par_total = 0
    for page_c in pages:
        if page_c:
            sh, sp = _course_shape(page_c)
            hole_count = max(hole_count, sh)
            par_total = max(par_total, sp)
    course_holes = hole_count
    if HOLE_COUNT_OVERRIDE is not None:
        hole_count = HOLE_COUNT_OVERRIDE

    players: list[dict] = []
    events: list[dict] = []
    for base_, page_c in zip(field_, pages):
        board_thru = base_.pop("boardThru", None)
        holes = _parse_holes(page_c) if page_c else []
        if HOLE_MAP:
            holes = [{**h, "hole": HOLE_MAP.get(h["hole"], h["hole"])} for h in holes]
        played = _played(holes)
        thru = _thru(holes, hole_count)
        # Board's Thru column override (fires when the board page has a Thru column)
        if board_thru is not None and board_thru > thru:
            thru = board_thru
        points = sum(h.get("points") or 0 for h in played)
        birdies = sum(1 for h in played if h.get("par") and isinstance(h.get("strokes"), int) and h["strokes"] < h["par"])
        last = played[-3:]
        p = {
            **base_,
            "thru": thru,
            "points": points,
            "birdies": birdies,
            "holes": holes,
            "last": [{"hole": h["hole"], "par": h.get("par"), "strokes": h.get("strokes"), "strokes2": h.get("strokes2"), "points": h.get("points"), "pointsSum": h.get("pointsSum")} for h in last],
            "_story": _story(played, is_stableford, base_["player"]),
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

    ranked = sorted(players, key=lambda p: (-p["points"], -p["thru"], p["player"]))
    for i, p in enumerate(ranked, 1):
        p["liveRank"] = i

    _tier_rank = {"gold": 3, "orange": 2, "red": 1, "blue": 0}
    stories = []
    for p in ranked:
        s = p.pop("_story", None)
        if s:
            stories.append({
                "player": p["player"],
                "title":  s["title"],
                "detail": s["detail"],
                "tier":   s["tier"],
                "emoji":  s.get("emoji", ""),
                "points": p["points"],
                "thru":   p["thru"],
            })
    stories.sort(key=lambda s: -_tier_rank.get(s["tier"], 0))

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
        "stories": stories[:12],
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
