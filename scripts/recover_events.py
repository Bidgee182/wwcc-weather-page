"""Recover past competition results by resolving member-portal event pages to
their report PDFs, parsing with the miscore parsers, and backfilling the matching
history archives (header-verified: only writes when the PDF's competition + date
match the archive, and only when it has MORE players than what's stored).

Usage:  python scripts/recover_events.py <eventId> [<eventId> ...]
Requires WWCC_USERNAME / WWCC_PASSWORD (same secrets as the leaderboard poll).
The event pages need a member login; the report PDFs themselves are public.
"""
import sys
import re
import io
import json
import glob
import pathlib
import urllib.request
from datetime import datetime

import pdfplumber

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from miscore.live import (  # noqa: E402
    _wwcc_login, _wwcc_get, _WWCC_BASE,
    _parse_pdf_standings, _parse_ntp_ld, _parse_ball_winners,
)

UA = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _iso(text: str):
    m = re.search(r"\w+day,\s+(\d{1,2}\s+\w+\s+20\d\d)", text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
        return r.read()


def _load_archives() -> dict:
    arcs = {}
    for f in glob.glob("out/history/*.json"):
        if f.endswith("index.json"):
            continue
        try:
            arcs[f] = json.load(open(f))
        except Exception:
            pass
    return arcs


def recover(event_ids: list[str]) -> int:
    jar = _wwcc_login()
    if jar is None:
        print("LOGIN FAILED - WWCC_USERNAME/WWCC_PASSWORD not set or rejected")
        return 1

    arcs = _load_archives()
    lr_path = pathlib.Path("out/last-results.json")
    lr = json.loads(lr_path.read_text()) if lr_path.exists() else {}
    changed = 0

    for eid in event_ids:
        page_url = f"{_WWCC_BASE}/members/golf/competition/CompetitionReporting?doAction=event&eventId={eid}"
        try:
            html = _wwcc_get(page_url, jar)
        except Exception as exc:
            print(f"event {eid}: page fetch error {exc}")
            continue
        # Collect any report PDF links on the page (absolute or relative).
        pdf_paths = set(re.findall(r'[^"\'\s>]*reportOutput/[^"\'\s>]*\.pdf', html))
        if not pdf_paths:
            print(f"event {eid}: no reportOutput PDF links found (page len {len(html)})")
            continue

        for path in pdf_paths:
            url = path if path.startswith("http") else _WWCC_BASE + ("" if path.startswith("/") else "/") + path.lstrip("/")
            try:
                b = _get_bytes(url)
            except Exception as exc:
                print(f"  {url.split('/')[-1]}: download error {exc}")
                continue
            if b[:5] != b"%PDF-":
                continue
            with pdfplumber.open(io.BytesIO(b)) as pdf:
                t = pdf.pages[0].extract_text() or ""
            comp = re.search(r"Competition\s*:\s*(.+)", t)
            di = _iso(t)
            if not comp or not di:
                continue
            pc = _norm(comp.group(1))
            st = _parse_pdf_standings(b)
            nl = _parse_ntp_ld(b)
            bw = _parse_ball_winners(b)
            np_ = len(st["players"])
            matched = False
            for f, d in arcs.items():
                if d.get("date") != di:
                    continue
                ac = _norm(d.get("competition"))
                if not (pc in ac or ac in pc):
                    continue
                matched = True
                cur = len((d.get("pdfStandings") or {}).get("players", []))
                if np_ > cur:
                    for p in st["players"]:
                        p["_is_prize_pdf"] = st.get("is_prize_pdf", False)
                    d["pdfStandings"] = {"grades": st.get("grades", []), "players": st["players"]}
                    d["ntpLd"] = nl["ntp"] + nl["ld"]
                    d["ballWinners"] = bw
                    json.dump(d, open(f, "w"), separators=(",", ":"))
                    changed += 1
                    print(f"  RECOVERED {di} {d['competition'][:30]:30} {cur}->{np_} players, {len(bw)} balls  [{url.split('/')[-1]}]")
                    if str(lr.get("leaderboardId")) == str(d.get("leaderboardId")):
                        lr["pdfStandings"] = d["pdfStandings"]; lr["ntpLd"] = d["ntpLd"]; lr["ballWinners"] = d["ballWinners"]
                        lr_path.write_text(json.dumps(lr, indent=1))
                else:
                    print(f"  skip      {di} {d['competition'][:30]:30} stored {cur} >= report {np_}")
                break
            if not matched:
                print(f"  no archive match for {di} '{comp.group(1).strip()}' ({url.split('/')[-1]})")

    print(f"\n{changed} archive(s) updated")
    return 0


if __name__ == "__main__":
    ids = sys.argv[1:] or ["10393107", "10393121", "10393127", "10393141"]
    raise SystemExit(recover(ids))
