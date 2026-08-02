"""
One-off backfill: re-scrape incomplete players from yesterday's men's comp and
poll the women's/4BBB companion boards, then save all to history.

Run via: python scripts/backfill_yesterday_scores.py
Requires: WWCC_USERNAME and WWCC_PASSWORD env vars (same as leaderboard poll).
"""
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from miscore.live import (
    find_companion_board, find_4bbb_board, poll,
    _fetch_card, _parse_holes, _played, _thru,
    HOLE_COUNT_OVERRIDE, HOLE_MAP,
)

CLUB        = "wwcc"
BOARD_ID    = "10414207"
BOARD_NAME  = "Saturday Mens Monthly Medal"
BOARD_DATE  = "2026-08-01"
HISTORY_DIR = pathlib.Path("out/history")
INDEX_PATH  = HISTORY_DIR / "index.json"
WORKERS     = 8

# ── helpers ──────────────────────────────────────────────────────────────────

def slim_player(p):
    d = {k: v for k, v in p.items() if k not in ("holes", "last")}
    if p.get("holes"):
        d["holePoints"]  = [h.get("points")  for h in p["holes"]]
        d["holePars"]    = [h.get("par")      for h in p["holes"]]
        d["holeStrokes"] = [h.get("strokes")  for h in p["holes"]]
        s2s = [h.get("strokes2") for h in p["holes"]]
        if any(s is not None for s in s2s):
            d["holeStrokes2"] = s2s
    return d

def make_index_entry(d, lb_id, archive_name):
    is_sf   = d.get("isStableford", True)
    players = d.get("players", [])
    leaders = d.get("leaders", [])
    if leaders:
        leader, leader_pts = leaders[0]["player"], leaders[0]["points"]
    elif players:
        sorted_p = sorted(players,
                          key=lambda p: -(p.get("points") or 0) if is_sf else (p.get("points") or 0))
        leader, leader_pts = sorted_p[0]["player"], sorted_p[0]["points"]
    else:
        leader, leader_pts = "", 0
    return {
        "date":          BOARD_DATE,
        "competition":   d.get("competition", ""),
        "type":          d.get("type"),
        "playerCount":   d.get("playerCount", len(players)),
        "holeCount":     d.get("holeCount", 0),
        "courseHoles":   d.get("courseHoles") or d.get("holeCount", 0),
        "leader":        leader,
        "leaderPts":     leader_pts,
        "leaderboardId": lb_id,
        "file":          f"out/history/{archive_name}",
    }

# ── Step 1: patch unfinished players in the men's comp ───────────────────────

men_path = HISTORY_DIR / f"{BOARD_DATE}-{BOARD_ID}.json"
with open(men_path) as f:
    men_data = json.load(f)

not_finished = [p for p in men_data["players"] if (p.get("thru") or 0) < (men_data.get("holeCount") or 18)]
print(f"Men's comp: {len(not_finished)} players to re-scrape")

hole_count  = men_data.get("holeCount") or 18
is_stableford = men_data.get("isStableford", False)

for idx, hist_player in enumerate(men_data["players"]):
    if (hist_player.get("thru") or 0) >= hole_count:
        continue
    pno  = hist_player.get("playerNo")
    name = hist_player["player"]
    if not pno:
        print(f"  SKIP {name}: no playerNo")
        continue
    print(f"  Re-scraping {name} (playerNo={pno})...", end=" ", flush=True)
    try:
        page = _fetch_card(CLUB, BOARD_ID, pno)
        if not page:
            print("no page returned")
            continue
        holes = _parse_holes(page)
        if HOLE_MAP:
            holes = [{**h, "hole": HOLE_MAP.get(h["hole"], h["hole"])} for h in holes]
        played_holes = _played(holes, is_stableford)
        thru = _thru(holes, hole_count, is_stableford)
        if is_stableford:
            points = sum(h.get("points") or 0 for h in played_holes)
        else:
            points = sum(0 if h.get("points") is None else h["points"] for h in played_holes)
        print(f"thru={thru}, pts={points}")
        # Update the player record in-place
        slim = slim_player({**hist_player,
                            "thru":   thru,
                            "points": points,
                            "holes":  holes})
        men_data["players"][idx].update(slim)
    except Exception as e:
        print(f"ERROR: {e}")

with open(men_path, "w") as f:
    json.dump(men_data, f, separators=(",", ":"))
print(f"Men's history updated: {men_path}")

# ── Step 2: poll companion boards and save to history ────────────────────────

primary_board = {
    "leaderboardId": BOARD_ID,
    "name":          BOARD_NAME,
    "date":          BOARD_DATE,
}
index = []
if INDEX_PATH.exists():
    with open(INDEX_PATH) as f:
        index = json.load(f)

for label, finder in (
    ("women's", find_companion_board),
    ("4BBB",    find_4bbb_board),
):
    cboard = finder(CLUB, primary_board, days=3)
    if not cboard:
        print(f"{label}: no companion board found")
        continue
    c_lb_id = cboard.get("leaderboardId")
    if not c_lb_id:
        print(f"{label}: no leaderboardId")
        continue
    if any(e.get("leaderboardId") == c_lb_id for e in index):
        print(f"{label}: already in index ({c_lb_id}), skipping")
        continue
    print(f"Polling {label} companion board {c_lb_id}...", flush=True)
    try:
        cb = poll(CLUB, cboard, WORKERS, {})
        is_sf       = cb.get("isStableford", True)
        players_out = cb.get("players", [])
        comp_type   = cb.get("type", "")

        c_data = {
            "competition":   cb.get("competition", cboard["name"]),
            "leaderboardId": c_lb_id,
            "date":          BOARD_DATE,
            "playerCount":   len(players_out),
            "holeCount":     cb.get("holeCount", 18),
            "courseHoles":   cb.get("courseHoles") or cb.get("holeCount", 18),
            "isStableford":  is_sf,
            "type":          comp_type,
            "par":           cb.get("par"),
            "pdfStandings":  cb.get("pdfStandings"),
            "ballWinners":   cb.get("ballWinners"),
            "players":       [slim_player(p) for p in players_out],
            "leaders":       [slim_player(p) for p in (cb.get("leaders") or [])],
        }
        c_archive_name = f"{BOARD_DATE}-{c_lb_id}.json"
        c_archive_path = HISTORY_DIR / c_archive_name
        with open(c_archive_path, "w") as f:
            json.dump(c_data, f, separators=(",", ":"))
        print(f"  {label} archived: {c_archive_path} ({c_archive_path.stat().st_size} bytes), {len(players_out)} players")
        index.insert(0, make_index_entry(c_data, c_lb_id, c_archive_name))
    except Exception as e:
        print(f"  {label} ERROR: {e}")

# Rebuild index entry for men's comp (it may have changed)
men_lb_id = BOARD_ID
men_archive_name = f"{BOARD_DATE}-{BOARD_ID}.json"
for i, e in enumerate(index):
    if e.get("leaderboardId") == men_lb_id:
        index[i] = make_index_entry(men_data, men_lb_id, men_archive_name)
        print(f"Men's index entry refreshed")
        break

with open(INDEX_PATH, "w") as f:
    json.dump(index, f, indent=2)
print(f"Index saved: {len(index)} entries")
print("Done.")
