"""
Retroactively fetch competition report PDFs for history entries that have
officialResultsReady=True but empty pdfStandings / ballWinners / ntpLd.

Run from repo root: python scripts/backfill_pdf_results.py
"""
import json, pathlib, sys, urllib.request, urllib.error, ssl
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from miscore.live import _parse_pdf_standings, _parse_ntp_ld

WWCC_BASE   = "https://wwcc.com.au"
HISTORY_DIR = pathlib.Path("out/history")
INDEX_PATH  = HISTORY_DIR / "index.json"
LAST_RESULTS = pathlib.Path("out/last-results.json")
CTX = ssl.create_default_context()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WWCCLeaderboard/1.0)",
    "Accept": "application/pdf,*/*",
}


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
        return r.read()


def backfill_comp(lb_id: str, archive_path: pathlib.Path, dry_run: bool = False) -> bool:
    direct_url = f"{WWCC_BASE}/upload/reportOutput/Golf_Competition_Report_{lb_id}.pdf"
    print(f"  Fetching {direct_url} ...")
    try:
        pdf_bytes = _fetch_bytes(direct_url)
        print(f"  Got {len(pdf_bytes)} bytes")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} - PDF not available")
        return False
    except Exception as e:
        print(f"  Fetch failed: {e}")
        return False

    ntp_ld_parsed = _parse_ntp_ld(pdf_bytes)
    standings     = _parse_pdf_standings(pdf_bytes)

    ball_winners: set = set()
    for p in standings.get("players", []):
        if not p.get("balls"):
            continue
        pname = p["name"]
        if ',' in pname:
            sp = pname.split(',', 1)
            pname = f'{sp[1].strip()} {sp[0].strip()}'
        if ' & ' not in pname and len(pname) >= 3:
            ball_winners.add(pname)

    print(f"  Grades: {standings.get('grades')}")
    print(f"  Players in PDF: {len(standings.get('players', []))}")
    print(f"  Ball winners: {sorted(ball_winners)}")
    print(f"  NTP/LD: {len(ntp_ld_parsed.get('ntp', []))} NTP, {len(ntp_ld_parsed.get('ld', []))} LD")

    if dry_run:
        return True

    with open(archive_path) as f:
        data = json.load(f)
    data["ballWinners"] = sorted(ball_winners)
    data["ntpLd"] = ntp_ld_parsed.get("ntp", []) + ntp_ld_parsed.get("ld", [])
    data["pdfStandings"] = standings
    with open(archive_path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"  Updated {archive_path}")
    return True


def main():
    with open(INDEX_PATH) as f:
        index = json.load(f)

    updated_any = False
    for entry in index:
        lb_id = str(entry.get("leaderboardId", ""))
        archive_path = pathlib.Path(entry.get("file", ""))
        if not lb_id or not archive_path.exists():
            continue

        with open(archive_path) as f:
            data = json.load(f)

        has_pdf = bool(data.get("pdfStandings", {}).get("players"))
        has_ball = bool(data.get("ballWinners"))
        has_ntp  = bool(data.get("ntpLd"))
        is_official = data.get("officialResultsReady", False)

        if is_official and not (has_pdf or has_ball or has_ntp):
            print(f"\n{entry['date']} {entry['competition']} (id={lb_id}) - missing PDF data")
            ok = backfill_comp(lb_id, archive_path)
            if ok:
                updated_any = True
        else:
            status = []
            if has_pdf:   status.append(f"{len(data['pdfStandings']['players'])} PDF players")
            if has_ball:  status.append(f"{len(data['ballWinners'])} ball winners")
            if has_ntp:   status.append(f"{len(data['ntpLd'])} NTP/LD")
            if not is_official:
                status.append("not official")
            print(f"{entry['date']} {entry['competition']}: {', '.join(status) or 'OK'}")

    # Update last-results.json if it matches a comp we just updated
    if updated_any and LAST_RESULTS.exists():
        lr = json.loads(LAST_RESULTS.read_text())
        lr_id = str(lr.get("leaderboardId", ""))
        matching = HISTORY_DIR / f"{lr.get('date', '')}-{lr_id}.json"
        if matching.exists():
            with open(matching) as f:
                updated = json.load(f)
            if updated.get("ballWinners") or updated.get("pdfStandings", {}).get("players"):
                lr["ballWinners"]  = updated.get("ballWinners", [])
                lr["ntpLd"]        = updated.get("ntpLd", [])
                lr["pdfStandings"] = updated.get("pdfStandings", {})
                LAST_RESULTS.write_text(json.dumps(lr, indent=1))
                print(f"\nUpdated last-results.json for {lr.get('competition')}")


if __name__ == "__main__":
    main()
