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
        cells = [c for c in cells if c]
        if not cells:
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
                current = {"hole_cols": [], "par": [], "strokes": [], "score": []}
                for i, v in enumerate(vals):
                    if re.fullmatch(r"\d+", v) and 1 <= int(v) <= 18:
                        current["hole_cols"].append((i, int(v)))
                nines.append(current)
            elif current is not None and label in ("par", "strokes", "score"):
                col_set = {i for i, _ in current["hole_cols"]}
                extracted = []
                for i, v in enumerate(vals):
                    if i in col_set:
                        extracted.append(int(v) if re.fullmatch(r"\d+", v) else None)
                current[label] = extracted

        result: list[dict] = []
        for nine in nines:
            holes = [h for _, h in nine["hole_cols"]]
            pars    = nine.get("par",     [])
            strokes = nine.get("strokes", [])
            points  = nine.get("score",   [])
            for i, hole in enumerate(holes):
                result.append({
                    "hole":    hole,
                    "par":     pars[i]    if i < len(pars)    else None,
                    "strokes": strokes[i] if i < len(strokes) else None,
                    "points":  points[i]  if i < len(points)  else None,
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
                else:
                    out.append(None)  # "-" or blank = not played
            return out[:-1] if out else []  # drop last = subtotal

        nines_pos: list[dict] = []
        cur: dict | None = None
        for label, vals in labeled:
            if label == "par":
                cur = {"par": vals, "strokes": [], "score": []}
                nines_pos.append(cur)
            elif cur is not None and label in ("strokes", "score"):
                cur[label] = vals

        out: list[dict] = []
        hole_num = 1
        for nine in nines_pos:
            pars    = _nine_vals(nine.get("par",     []))
            strokes = _nine_vals(nine.get("strokes", []))
            points  = _nine_vals(nine.get("score",   []))
            for i in range(len(pars)):
                out.append({
                    "hole":    hole_num,
                    "par":     pars[i]    if i < len(pars)    else None,
                    "strokes": strokes[i] if i < len(strokes) else None,
                    "points":  points[i]  if i < len(points)  else None,
                })
                hole_num += 1
        return out
