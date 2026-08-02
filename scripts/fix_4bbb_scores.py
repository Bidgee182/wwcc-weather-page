"""
Fix 2026-08-01 4BBB history: re-poll board 10414193 as Stableford (no stroke conversion).
PDF confirms: Saturday 4BBB Stableford, winner Melanie Cramp & Luisa Bertoldi 78pts.
"""
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from miscore.live import poll, _fetch_card, _parse_holes, HOLE_MAP

CLUB        = "wwcc"
BOARD_ID    = "10414193"
BOARD_DATE  = "2026-08-01"
HISTORY_DIR = pathlib.Path("out/history")
INDEX_PATH  = HISTORY_DIR / "index.json"
WORKERS     = 8

board = {
    "leaderboardId": BOARD_ID,
    "name":          "Saturday 4BBB Stableford",
    "date":          BOARD_DATE,
}

print(f"Re-polling 4BBB board {BOARD_ID}...", flush=True)
cb = poll(CLUB, board, WORKERS, {})

is_sf       = cb.get("isStableford", True)
comp_type   = cb.get("type", "")
players_out = cb.get("players", [])

print(f"isStableford={is_sf}, type={comp_type}, players={len(players_out)}")
if players_out:
    top5 = sorted(players_out, key=lambda p: -(p.get("points") or 0))[:5]
    for p in top5:
        print(f"  {p['player']} | pts={p.get('points')} | thru={p.get('thru')}")

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

c_data = {
    "competition":  cb.get("competition", board["name"]),
    "leaderboardId": BOARD_ID,
    "date":         BOARD_DATE,
    "playerCount":  len(players_out),
    "holeCount":    cb.get("holeCount", 18),
    "courseHoles":  cb.get("courseHoles") or cb.get("holeCount", 18),
    "isStableford": is_sf,
    "type":         comp_type,
    "par":          cb.get("par"),
    "pdfStandings": cb.get("pdfStandings"),
    "ballWinners":  cb.get("ballWinners"),
    "players":      [slim_player(p) for p in players_out],
    "leaders":      [slim_player(p) for p in (cb.get("leaders") or [])],
}

archive_name = f"{BOARD_DATE}-{BOARD_ID}.json"
archive_path = HISTORY_DIR / archive_name
with open(archive_path, "w") as f:
    json.dump(c_data, f, separators=(",", ":"))
print(f"Saved {archive_path} ({archive_path.stat().st_size} bytes)")

# Update index entry
index = json.load(open(INDEX_PATH))
top_players = c_data["leaders"] or sorted(
    players_out, key=lambda p: -(p.get("points") or 0)
)
leader = top_players[0]["player"] if top_players else ""
leader_pts = top_players[0]["points"] if top_players else 0

new_entry = {
    "date":          BOARD_DATE,
    "competition":   c_data["competition"],
    "type":          c_data["type"],
    "playerCount":   c_data["playerCount"],
    "holeCount":     c_data["holeCount"],
    "courseHoles":   c_data["courseHoles"],
    "leader":        leader,
    "leaderPts":     leader_pts,
    "leaderboardId": BOARD_ID,
    "file":          f"out/history/{archive_name}",
}

# Replace existing entry or insert
for i, e in enumerate(index):
    if e.get("leaderboardId") == BOARD_ID:
        index[i] = new_entry
        print(f"Updated index entry: {leader} {leader_pts}")
        break
else:
    index.insert(0, new_entry)
    print(f"Inserted index entry: {leader} {leader_pts}")

with open(INDEX_PATH, "w") as f:
    json.dump(index, f, indent=2)
print("Done.")
