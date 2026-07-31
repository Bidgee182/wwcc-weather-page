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
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import sleep

from .webscrape import BASE, _get, _parse_holes, list_competitions

log = logging.getLogger("miscore.live")

def _git_hash() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip() or None
    except Exception:
        return None

def _git_remote_hash() -> str | None:
    """Latest commit on origin/main - tells the kiosk if the poller is behind GitHub."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "origin/main"],
            stderr=subprocess.DEVNULL,
        ).decode().strip() or None
    except Exception:
        return None

GIT_HASH: str | None = _git_hash()
GIT_REMOTE_HASH: str | None = _git_remote_hash()

HOLE_MAP: dict[int, int] = {}
HOLE_COUNT_OVERRIDE: int | None = None

# Day-of stories archive: keyed "player|title", reset when competition changes.
_stories_archive: dict[str, dict] = {}
_archive_board_id: str | None = None

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
        # Blank holes sandwiched between played holes whose par IS populated are
        # NR pickups - the scorer left strokes and score empty but the hole par
        # is still shown. Holes with par=None are genuinely unplayed (MiClub
        # does not pre-fill par for holes the player hasn't reached yet).
        min_p, max_p = min(played_nums), max(played_nums)
        nr_gaps = sum(1 for h in holes
                      if min_p < h["hole"] < max_p
                      and h["hole"] not in played_nums
                      and h.get("par") is not None)
        total = played_count + nr_gaps
        if total >= hole_count:
            return hole_count
        return total
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

    def _pick(combos, tier, emoji):
        t, d = combos[abs(hash(player)) % len(combos)]
        return story(t, d, tier, emoji)

    # ── Per-hole priority stories ────────────────────────────────────────────

    # Hole in one
    for h in played:
        if h.get("strokes") == 1 and h.get("par") == 3:
            return story("ACE!", f"Hole in one on hole {hnum(h)} - buy them a drink!", "gold", "🎯")

    # Two or more eagles in the round
    eagle_holes = [h for h in played if is_eagle(h)]
    if len(eagle_holes) >= 2:
        big = f"{len(eagle_holes)} big ones" if is_stableford else f"{len(eagle_holes)} eagles"
        hs = holes_str(eagle_holes)
        return _pick([
            ("Someone Call Security", f"{big} on {hs} - the handicapper has questions"),
            ("Absolute Menace", f"{big} on {hs} - the rest of us should consider going home"),
            ("Not Our Problem", f"{big} on {hs} - the course just surrendered"),
        ], "gold", "🚔")

    # Consecutive birdie streak from most recent hole backward
    birdie_streak = 0
    for h in reversed(played):
        if is_birdie(h): birdie_streak += 1
        else: break
    streak_holes = played[-birdie_streak:] if birdie_streak else []

    if birdie_streak >= 3:
        return _pick([
            ("Running Hot", f"{birdie_streak} straight {birdies_word()} on {holes_str(streak_holes)}"),
            ("Cool It Down", f"{birdie_streak} in a row on {holes_str(streak_holes)} - is this even legal?"),
            ("Scoreboard Bully", f"{birdie_streak} {birdies_word()} straight - someone else's problem now"),
        ], "gold", "🔥")
    if birdie_streak >= 2:
        return _pick([
            ("On the Charge", f"Back-to-back {birdies_word()} on {holes_str(streak_holes)}"),
            ("Going Rogue", f"Back-to-back on {holes_str(streak_holes)} - the field is on notice"),
            ("Two's Company", f"{birdies_word()} on {holes_str(streak_holes)} - this is momentum"),
        ], "orange", "⚡")

    # Eagle on most recent hole
    if is_eagle(played[-1]):
        last_h = played[-1]
        if n >= 3 and is_wipe(played[-2]) and is_wipe(played[-3]):
            three = holes_str([played[-3], played[-2], last_h])
            return _pick([
                ("Out of Nowhere", f"Two wipes then {eagle_word(last_h)} on {three}"),
                ("Jekyll and Hyde", f"Two wipes then {eagle_word(last_h)} on {three} - pick a personality"),
                ("The Plot Twist", f"Two wipes, one {eagle_word(last_h)} on {three} - nobody ordered that"),
            ], "gold", "🎭")
        return _pick([
            ("The Big Gun", f"{eagle_word(last_h)} on hole {hnum(last_h)}"),
            ("Don't Mind Me", f"{eagle_word(last_h)} on hole {hnum(last_h)} - just casually"),
            ("Case Closed", f"{eagle_word(last_h)} on hole {hnum(last_h)} - that'll do nicely"),
        ], "gold", "💥")

    # Wipe streak
    wipe_streak = 0
    for h in reversed(played):
        if is_wipe(h): wipe_streak += 1
        else: break
    wipe_holes = played[-wipe_streak:] if wipe_streak else []
    if wipe_streak >= 4:
        return _pick([
            ("Already at the Bar", f"{wipe_streak} wipes in a row on {holes_str(wipe_holes)}"),
            ("Donating Points to the Course", f"{wipe_streak} straight wipes - the course says thank you"),
            ("Points? What Points?", f"{wipe_streak} in a row on {holes_str(wipe_holes)} - the scorecard looks very clean"),
        ], "red", "🍺")
    if wipe_streak == 3:
        return _pick([
            ("Left the Clubs at Home", f"3 wipes in a row on {holes_str(wipe_holes)}"),
            ("Technical Difficulties", f"3 wipes on {holes_str(wipe_holes)} - please stand by"),
            ("The Course Wins Again", f"3 straight on {holes_str(wipe_holes)} - the handicap card stays in the bag"),
        ], "red", "🏠")
    if wipe_streak == 2 and n >= 5:
        return _pick([
            ("Rough Patch", f"2 wipes on {holes_str(wipe_holes)} - club still in the bag"),
            ("Brief Interruption", f"Back-to-back wipes on {holes_str(wipe_holes)} - we'll pretend this didn't happen"),
            ("Plot Hole", f"2 wipes on {holes_str(wipe_holes)} - the scorecard disagrees with the player"),
        ], "red", "🩹")

    # Bogey train (escalating shade)
    bogey_streak = 0
    for h in reversed(played):
        if is_bogey(h): bogey_streak += 1
        else: break
    bogey_holes = played[-bogey_streak:] if bogey_streak else []
    if bogey_streak >= 6:
        return _pick([
            ("Someone Check On Them", f"{bogey_streak} {bogeys_word()} in a row on {holes_str(bogey_holes)} - pure Sunday arvo golf energy"),
            ("We've Lost Them", f"{bogey_streak} in a row on {holes_str(bogey_holes)} - send water, send snacks, send help"),
            ("The Bogey Cascade", f"{bogey_streak} {bogeys_word()} straight - the handicap is having a moment"),
        ], "red", "🚑")
    if bogey_streak >= 5:
        return _pick([
            ("Is This Fun Anymore?", f"{bogey_streak} {bogeys_word()} on the bounce on {holes_str(bogey_holes)} - character-building stuff"),
            ("The Bogey Buffet", f"{bogey_streak} in a row on {holes_str(bogey_holes)} - help yourself, there are plenty"),
            ("Stay Strong", f"{bogey_streak} {bogeys_word()} straight on {holes_str(bogey_holes)} - the commentators have gone quiet"),
        ], "red", "😭")
    if bogey_streak >= 4:
        return _pick([
            ("Still Grinding...", f"{bogey_streak} straight {bogeys_word()} on {holes_str(bogey_holes)}"),
            ("Technically Still Scoring", f"{bogey_streak} {bogeys_word()} in a row - at least it's not a wipe"),
            ("1-Pointer Mode Activated", f"{bogey_streak} in a row on {holes_str(bogey_holes)} - consistent, at least"),
        ], "blue", "😤")
    if bogey_streak >= 3:
        return _pick([
            ("The Grind", f"{bogey_streak} {bogeys_word()} in a row on {holes_str(bogey_holes)}"),
            ("Perfectly Mediocre", f"{bogey_streak} {bogeys_word()} straight - the definition of par plus one"),
            ("Bogey Parade", f"{bogey_streak} in a row on {holes_str(bogey_holes)} - sticking to the 1-point script"),
        ], "blue", "⛏️")

    # Two-hole transitions
    if n >= 2:
        prev_h, last_h = played[-2], played[-1]
        two = holes_str([prev_h, last_h])
        if is_eagle(prev_h) and is_wipe(last_h):
            return _pick([
                ("The Rollercoaster", f"{eagle_word(prev_h)} then a wipe on {two} - peak Australian sport"),
                ("Brilliant and Then Not", f"{eagle_word(prev_h)} on hole {hnum(prev_h)}, wipe on hole {hnum(last_h)} - the heart rate approves"),
                ("High Low Express", f"From {eagle_word(prev_h)} to wipe in one hole - nobody does it like this"),
            ], "orange", "🎢")
        if is_birdie(prev_h) and is_wipe(last_h):
            return _pick([
                ("Hero to Zero", f"{birdie_word(prev_h)} then a wipe on {two} - feast then famine"),
                ("Speed Bump", f"{birdie_word(prev_h)} on hole {hnum(prev_h)}, wipe on hole {hnum(last_h)} - the course corrects"),
                ("The Golf Tax", f"One good hole then one bad on {two} - the course charges a fee for everything"),
            ], "orange", "📉")
        if gpts(prev_h) == 3 and is_bogey(last_h):
            _bw = birdie_word(prev_h)
            _bo = bogey_word()
            return _pick([
                ("Giveth and Taketh Away", f"{_bw} straight into a {_bo} on {two}"),
                ("One Step Forward...", f"{_bw} on hole {hnum(prev_h)}, {_bo} on hole {hnum(last_h)}"),
                ("The Golf Tax Strikes Again", f"{_bw} to {_bo} on {two} - the course always takes its cut"),
            ], "orange", "🔄")
        if is_wipe(prev_h) and is_birdie(last_h):
            return _pick([
                ("The Bounce Back", f"Wipe then a {birdie_word(last_h)} on {two}"),
                ("Not Done Yet", f"Wipe on hole {hnum(prev_h)}, {birdie_word(last_h)} on hole {hnum(last_h)} - that's better"),
                ("The Phoenix", f"Wipe then {birdie_word(last_h)} on {two} - drama then redemption"),
            ], "orange", "↩️")

    # Par streak
    par_streak = 0
    for h in reversed(played):
        if is_par(h): par_streak += 1
        else: break
    par_holes = played[-par_streak:] if par_streak else []
    if par_streak >= 9:
        return _pick([
            ("The Metronome", f"{par_streak} pars on {holes_str(par_holes)}. Just. Pars."),
            ("Par Machine", f"{par_streak} in a row on {holes_str(par_holes)} - no surprises offered, none taken"),
            ("Keeping the Fairway Warm", f"{par_streak} straight pars on {holes_str(par_holes)} - steady as a heartbeat"),
        ], "blue", "⏱️")
    if par_streak >= 7:
        return _pick([
            ("Human Highway", f"{par_streak} pars in a row on {holes_str(par_holes)} - accountant energy"),
            ("Pleasantly Predictable", f"{par_streak} pars on {holes_str(par_holes)} - the card is getting boring in the best way"),
            ("The Flat White Round", f"{par_streak} pars in a row on {holes_str(par_holes)} - reliable, consistent, zero drama"),
        ], "blue", "🛣️")
    if par_streak >= 5:
        return _pick([
            ("Vanilla Golf", f"{par_streak} straight pars on {holes_str(par_holes)}"),
            ("Textbook Stuff", f"{par_streak} pars in a row on {holes_str(par_holes)} - not a bad thing, actually"),
            ("The Null Hypothesis", f"{par_streak} pars on {holes_str(par_holes)} - the scorecard is a flatline"),
        ], "blue", "🍦")
    if par_streak >= 3:
        return _pick([
            ("Finding a Rhythm", f"{par_streak} pars on the bounce on {holes_str(par_holes)}"),
            ("Settling In", f"{par_streak} pars in a row on {holes_str(par_holes)} - starting to look comfortable"),
            ("Building Something", f"{par_streak} pars on {holes_str(par_holes)} - the round is taking shape"),
        ], "blue", "🎵")

    # Bad start: wipes from the very first hole
    wipes_from_start = 0
    for h in played:
        if is_wipe(h): wipes_from_start += 1
        else: break
    start_holes = played[:wipes_from_start]
    if wipes_from_start >= 7:
        return _pick([
            ("Save It For Next Week", f"Still searching - {wipes_from_start} wipes to open on {holes_str(start_holes)}"),
            ("Cold Start Doesn't Cover It", f"{wipes_from_start} wipes from the gun - the warm-up is not helping"),
            ("The Engine Won't Turn Over", f"{wipes_from_start} straight wipes to start - the round needs a jump start"),
        ], "red", "📅")
    if wipes_from_start >= 5:
        return _pick([
            ("Looking for the Course", f"Nothing from the first {wipes_from_start} on {holes_str(start_holes)}"),
            ("GPS Required", f"{wipes_from_start} opening wipes on {holes_str(start_holes)} - still searching for the fairway"),
            ("Wrong Track from the Start", f"{wipes_from_start} wipes to open - at least it can only improve"),
        ], "red", "🗺️")
    if wipes_from_start >= 3 and n <= 9:
        return _pick([
            ("Slow Starter", f"Nil from the first {wipes_from_start} on {holes_str(start_holes)}"),
            ("Getting Warmed Up", f"{wipes_from_start} wipes to start on {holes_str(start_holes)} - it's still early"),
            ("The Delayed Arrival", f"{wipes_from_start} wipes to open on {holes_str(start_holes)} - the real round starts now"),
        ], "blue", "🐢")

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
        return _pick([
            ("The Colander", f"{total_wipes} wipes from {n} holes - more gaps than a country fence"),
            ("The Points Drought", f"{total_wipes} wipes from {n} holes - officially requesting emergency services"),
            ("The Sieve", f"{total_wipes} wipes in {n} holes - the points have somewhere better to be"),
        ], "red", "🪣")
    if total_wipes >= 6:
        return _pick([
            ("Points-Free Diet", f"{total_wipes} wipes - the fairways are winning today"),
            ("No Points No Problem... Actually Wait", f"{total_wipes} wipes - the course is undefeated today"),
            ("The Scoreless Wander", f"{total_wipes} wipes from the card - cutting carbs, apparently"),
        ], "red", "🌵")
    if total_wipes >= 4 and n >= 9:
        return _pick([
            ("The Streaker (Not That Kind)", f"{total_wipes} wipes in {n} holes - tough day at the office"),
            ("Not Today, Scoreboard", f"{total_wipes} wipes from {n} holes - the scorecard is mostly fresh air"),
            ("Wipe Artist", f"{total_wipes} wipes in {n} holes - technically a very consistent performance"),
        ], "red", "😵")

    # Bogey storms (longest run anywhere, not just current tail)
    longest_bogey_run = _max_run(is_bogey)
    if longest_bogey_run >= 5:
        return _pick([
            ("Kick, Chase, Repeat", f"{longest_bogey_run} {bogeys_word()} in a row at some point - always knocking, never scoring"),
            ("One-Point Club", f"{longest_bogey_run} {bogeys_word()} in a row somewhere on the card - par is a distant memory"),
            ("The Long Bogey", f"{longest_bogey_run} {bogeys_word()} straight at some point - it happened and it hurt"),
        ], "red", "🦶")
    if total_bogeys >= 9:
        return _pick([
            ("The 1-Pointer Specialist", f"{total_bogeys} {bogeys_word()} on the card - the bogey machine is fully operational"),
            ("Committed to the Bogey", f"{total_bogeys} {bogeys_word()} - every hole is a 1-pointer and that is the plan"),
            ("Very Consistent, Very Bogey", f"{total_bogeys} {bogeys_word()} on the card - nailed the brief"),
        ], "blue", "🔩")
    if total_bogeys >= 7 and total_wipes == 0:
        return _pick([
            ("Nothing But Bogeys", f"{total_bogeys} {bogeys_word()}, zero wipes - zero wipes is something to hang your hat on"),
            ("Clean but Not Pretty", f"{total_bogeys} {bogeys_word()} and not a single wipe - disciplined mediocrity"),
            ("The Bogey Purist", f"{total_bogeys} {bogeys_word()}, zero wipes - a very niche skillset"),
        ], "blue", "⚙️")

    # Total birdies haul (not bunched - streaks already caught above)
    bw = "3-pointers+" if is_stableford else "birdies"
    if total_birdies >= 5:
        return _pick([
            ("The Merchant", f"{total_birdies} {bw} on the card - all over the card in the best possible way"),
            ("Open for Business", f"{total_birdies} {bw} from {n} holes - finding the scoreboard everywhere"),
            ("The Birdie Farmer", f"{total_birdies} {bw} harvested from the round - relentless"),
        ], "orange", "🛍️")
    if total_birdies >= 3 and n >= 12:
        return _pick([
            ("Spot Fires", f"{total_birdies} {bw} scattered through the round - keeps finding ways to score"),
            ("Under the Radar", f"{total_birdies} {bw} through the round - not flashy, just effective"),
            ("The Opportunist", f"{total_birdies} {bw} from {n} holes - takes the chances when they come"),
        ], "orange", "✨")

    # Searching - wipes with no birdies
    if total_wipes >= 3 and total_birdies == 0 and n >= 9:
        return _pick([
            ("Still Searching", f"{total_wipes} wipes, no birdies from {n} holes - the flagstick is definitely moving"),
            ("Points: Not Found", f"{total_wipes} wipes, zero birdies from {n} holes - the course hides its rewards well"),
            ("The Hunt Continues", f"{total_wipes} wipes and still no birdies from {n} holes - they will come eventually"),
        ], "red", "🔍")

    # Clean card - no wipes
    if total_wipes == 0 and n >= 12:
        return _pick([
            ("Squeaky Clean", f"Zero wipes from {n} holes - not a wipe in sight"),
            ("The Impossibility", f"Zero wipes from {n} holes - is this card even theirs?"),
            ("Pristine", f"Zero wipes from {n} holes - the scorer has not written a zero yet"),
        ], "blue", "🧹")

    # Scoring average
    if n >= 9:
        avg = total_pts / n
        if avg >= 2.8:
            return _pick([
                ("That's a Good Card", f"Averaging {avg:.1f}pts per hole through {n} - controlled, methodical, relentless"),
                ("Numbers Don't Lie", f"Averaging {avg:.1f}pts a hole through {n} - this is very good"),
                ("The Efficiency Machine", f"{avg:.1f}pts per hole from {n} - every shot is doing its job"),
            ], "gold", "📈")
        if avg >= 2.4:
            return _pick([
                ("On the Right Track", f"Averaging {avg:.1f}pts per hole through {n} - not flashy but solid"),
                ("Quietly Getting There", f"{avg:.1f}pts a hole through {n} - flying under the radar"),
                ("Steady Progress", f"Averaging {avg:.1f}pts from {n} holes - the engine is warm"),
            ], "orange", "🛤️")
        if avg < 0.8:
            return _pick([
                ("Lost in the Rough", f"Under a point a hole through {n} - the course is not cooperating"),
                ("Points Are Hard", f"{avg:.1f}pts per hole from {n} - they make it look very difficult"),
                ("Character Building", f"Under a point a hole through {n} - what doesn't kill you, etc."),
            ], "red", "🌿")

    # All four outcomes seen (full experience)
    if total_wipes > 0 and total_bogeys > 0 and total_pars > 0 and total_birdies > 0:
        return _pick([
            ("The Complete Package", f"Wipes, bogeys, pars AND birdies - a round that covers all four food groups"),
            ("The Full Experience", f"Hit every outcome on the card - the golf round as a grab bag"),
            ("Variety Pack", f"Wipes, bogeys, pars, birdies - something for everyone on this card"),
        ], "blue", "🎰")

    # Pure bogeys only - no other outcomes
    if total_bogeys >= 5 and total_wipes == 0 and total_birdies == 0 and n >= 9:
        return _pick([
            ("The Consistent Battler", f"{total_bogeys} {bogeys_word()} and nothing else - exactly what it says on the tin"),
            ("One-Trick Pony (Technically Solid)", f"{total_bogeys} {bogeys_word()} - found a formula and sticking to it"),
            ("Pure Bogey", f"{total_bogeys} {bogeys_word()} and nothing else from {n} holes - no deviations from the plan"),
        ], "blue", "🔩")

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

    # Accumulate the day-of stories archive; reset on new competition.
    global _stories_archive, _archive_board_id
    if board_id != _archive_board_id:
        _stories_archive.clear()
        _archive_board_id = board_id
    now_iso = now.isoformat()
    for s in stories:
        key = s["player"] + "|" + s["title"]
        if key not in _stories_archive:
            _stories_archive[key] = {**s, "firstSeen": now_iso}
        else:
            _stories_archive[key]["points"] = s["points"]
            _stories_archive[key]["thru"]   = s["thru"]
    archive_list = sorted(_stories_archive.values(), key=lambda x: x["firstSeen"])

    return {
        "competition": board["name"],
        "type": comp_type,
        "date": board.get("date"),
        "leaderboardId": board_id,
        "holeCount": hole_count or None,
        "courseHoles": course_holes or hole_count or None,
        "par": par_total or None,
        "gitHash": GIT_HASH,
        "gitRemoteHash": GIT_REMOTE_HASH,
        "generatedAt": now.isoformat(),
        "playerCount": len(players),
        "started": any(p["thru"] > 0 for p in players),
        "players": ranked,
        "leaders": ranked[:10],
        "stories": stories[:12],
        "storiesArchive": archive_list,
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
