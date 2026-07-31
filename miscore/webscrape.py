"""HTTP fetch and HTML parsing helpers shared by miscore tools."""

import html as _html
import re
import urllib.request
from datetime import datetime

BASE = "https://leaderboard.miclub.com.au"

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WWCC-Kiosk/1.0)"}


def _get(url: str) -> str:
    """HTTP GET, returns decoded text. Raises on error."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def list_competitions(club: str, days: int = 3) -> list[dict]:
    """Return [{leaderboardId, name, date}] in page order (newest first)."""
    page = _get(f"{BASE}/show-leaderboards?club={club}&days={days}")
    comps = []
    for m in re.finditer(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        row = m.group(1)
        link = re.search(
            r'display-leaderboard\?[^"]*leaderboardId=(\d+)[^"]*"[^>]*>([^<]+)<', row
        )
        if not link:
            continue
        lb_id = link.group(1)
        name  = _html.unescape(link.group(2)).strip()
        # Gender is in a separate <td> (e.g. "Womens", "Mens") - append if not "All"
        tds = re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)
        for td in tds:
            gender = _html.unescape(re.sub(r"<[^>]+>", "", td)).strip()
            if gender in ("Womens", "Mens", "Ladies", "Mixed"):
                name = name + " " + gender
                break
        date_m = re.search(r"(\d{2}/\d{2}/\d{4})", row)
        date_str = None
        if date_m:
            try:
                date_str = datetime.strptime(date_m.group(1), "%d/%m/%Y").date().isoformat()
            except ValueError:
                pass
        comps.append({"leaderboardId": lb_id, "name": name, "date": date_str})
    return comps


def _parse_holes(page: str) -> list[dict]:
    """Parse a scorecard page into [{hole, par, strokes, points}].

    Handles two scorecard formats:
    - With a 'Hole' header row: use column-index alignment to skip subtotals.
    - Without a 'Hole' header row (WWCC format): group Par/Strokes/Score rows
      into nines; the last value in each row is the nine subtotal and is dropped.
    """
    # Collect labeled rows: [(label, [cell, ...])]
    labeled: list[tuple[str, list[str]]] = []
    for m in re.finditer(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        cells = [
            _html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
            for c in re.findall(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>", m.group(1))
        ]
        if not any(cells):  # skip rows that are entirely empty
            continue
        label = cells[0].lower().strip()
        if label in ("hole", "par", "strokes", "score"):
            labeled.append((label, cells[1:]))

    if not labeled:
        return []

    has_hole_row = any(lbl == "hole" for lbl, _ in labeled)

    if has_hole_row:
        # Column-index approach: use the Hole row to identify which columns
        # are real holes (1-18) vs subtotal columns.
        nines: list[dict] = []
        current: dict | None = None
        for label, vals in labeled:
            if label == "hole":
                hole_cols = []
                hole_nums = []
                for i, v in enumerate(vals):
                    if re.fullmatch(r"\d+", v) and 1 <= int(v) <= 18:
                        hole_cols.append((i, int(v)))
                        hole_nums.append(int(v))
                # 4BBB scorecards have two full player sections; if these holes
                # were already parsed, reuse that nine so data merges into
                # strokes2/score2 rather than creating duplicate hole entries.
                existing = next((n for n in nines if [h for _, h in n["hole_cols"]] == hole_nums), None)
                if existing:
                    current = existing
                else:
                    current = {"hole_cols": hole_cols, "par": [], "strokes": [], "score": []}
                    nines.append(current)
            elif current is not None and label in ("par", "strokes", "score"):
                col_set = {i for i, _ in current["hole_cols"]}
                extracted = []
                for i, v in enumerate(vals):
                    if i in col_set:
                        if re.fullmatch(r"\d+", v):
                            extracted.append(int(v))
                        elif v == "-":
                            extracted.append(False)  # explicit dash = played, no value
                        else:
                            extracted.append(None)   # blank = not yet played
                # 4BBB scorecards have two strokes rows and two score rows (one per player)
                if label == "strokes" and current["strokes"]:
                    current["strokes2"] = extracted
                elif label == "score" and current["score"]:
                    current["score2"] = extracted
                else:
                    current[label] = extracted

        result: list[dict] = []
        for nine in nines:
            holes    = [h for _, h in nine["hole_cols"]]
            pars     = nine.get("par",      [])
            strokes  = nine.get("strokes",  [])
            strokes2 = nine.get("strokes2", [])
            points   = nine.get("score",    [])
            points2  = nine.get("score2",   [])
            for i, hole in enumerate(holes):
                p1_raw = points[i]   if i < len(points)   else None
                p2_raw = points2[i]  if i < len(points2)  else None
                s1_raw = strokes[i]  if i < len(strokes)  else None
                s2_raw = strokes2[i] if i < len(strokes2) else None
                par_raw = pars[i]    if i < len(pars)     else None
                # Explicit dash in score row = pickup = 0 stableford pts
                p1 = 0 if p1_raw is False else p1_raw
                p2 = 0 if p2_raw is False else p2_raw
                # Explicit dash in strokes row = played but no stroke count
                s1 = None if s1_raw is False else s1_raw
                s2 = None if s2_raw is False else s2_raw
                # 4BBB: take better score per hole; single-player: p2 is absent
                pts = max(p1, p2) if p1 is not None and p2 is not None else (p1 if p1 is not None else p2)
                # pointsSum = combined individual scores (used for 4BBB heater threshold)
                pts_sum = (p1 or 0) + (p2 or 0) if (p1 is not None or p2 is not None) else None
                # "played" = score/points row was explicitly entered (numeric or '-').
                # Strokes row alone does NOT count: MiClub pre-fills strokes with '-'
                # for all 18 holes before a player tees off, which would falsely mark
                # unplayed holes as played if we included s1_raw/s2_raw here.
                played_flag = (p1_raw is not None or p2_raw is not None)
                result.append({
                    "hole":      hole,
                    "par":       None if par_raw is False else par_raw,
                    "strokes":   s1,
                    "strokes2":  s2,
                    "points":    pts,
                    "pointsSum": pts_sum,
                    "played":    played_flag,
                })
        return result

    else:
        # Positional approach: each Par row starts a new nine; the last value
        # in every row is the nine subtotal - drop it to get per-hole values.
        def _nine_vals(vals: list[str]) -> list:
            out = []
            for v in vals:
                if re.fullmatch(r"\d+", v):
                    out.append(int(v))
                elif v == "-":
                    out.append(False)   # explicit dash = played but no value
                else:
                    out.append(None)    # blank = not yet played
            return out[:-1] if out else []  # drop last = nine subtotal

        nines_pos: list[dict] = []
        cur: dict | None = None
        for label, vals in labeled:
            if label == "par":
                # 4BBB scorecards have two full player sections with repeated par rows.
                # If these exact par values were already seen, reuse that nine so
                # subsequent strokes/score rows merge into strokes2/score2 rather
                # than creating duplicate hole entries.
                existing = next((n for n in nines_pos if n["par"] == vals), None)
                if existing:
                    cur = existing
                else:
                    cur = {"par": vals, "strokes": [], "score": []}
                    nines_pos.append(cur)
            elif cur is not None and label in ("strokes", "score"):
                # 4BBB scorecards have two strokes rows and two score rows (one per player)
                if label == "strokes" and cur["strokes"]:
                    cur["strokes2"] = vals
                elif label == "score" and cur["score"]:
                    cur["score2"] = vals
                else:
                    cur[label] = vals

        out: list[dict] = []
        hole_num = 1
        for nine in nines_pos:
            pars     = _nine_vals(nine.get("par",      []))
            strokes  = _nine_vals(nine.get("strokes",  []))
            strokes2 = _nine_vals(nine.get("strokes2", []))
            points   = _nine_vals(nine.get("score",    []))
            points2  = _nine_vals(nine.get("score2",   []))
            for i in range(len(pars)):
                p1_raw = points[i]   if i < len(points)   else None
                p2_raw = points2[i]  if i < len(points2)  else None
                s1_raw = strokes[i]  if i < len(strokes)  else None
                s2_raw = strokes2[i] if i < len(strokes2) else None
                par_raw = pars[i]    if i < len(pars)     else None
                # Explicit dash in score row = pickup = 0 stableford pts
                p1 = 0 if p1_raw is False else p1_raw
                p2 = 0 if p2_raw is False else p2_raw
                # Explicit dash in strokes row = played but no stroke count
                s1 = None if s1_raw is False else s1_raw
                s2 = None if s2_raw is False else s2_raw
                pts = max(p1, p2) if p1 is not None and p2 is not None else (p1 if p1 is not None else p2)
                pts_sum = (p1 or 0) + (p2 or 0) if (p1 is not None or p2 is not None) else None
                # Score/points row only: MiClub pre-fills strokes with '-' before play
                played_flag = (p1_raw is not None or p2_raw is not None)
                out.append({
                    "hole":      hole_num,
                    "par":       None if par_raw is False else par_raw,
                    "strokes":   s1,
                    "strokes2":  s2,
                    "points":    pts,
                    "pointsSum": pts_sum,
                    "played":    played_flag,
                })
                hole_num += 1
        return out
