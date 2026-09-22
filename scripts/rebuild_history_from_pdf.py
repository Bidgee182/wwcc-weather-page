#!/usr/bin/env python3
"""
Rebuild past-results history blobs from the OFFICIAL competition PDF.

Why: the 1am archiver froze some comps in a pre-round state (all players thru=0,
0 points, empty pdfStandings) and the "already archived" guard then blocked the
real results from ever replacing them - so the Past Results page shows the whole
field on 0. The official PDF is the durable source of truth and is re-fetchable
by board id, so we rebuild the blob's player points/thru + pdfStandings +
ballWinners from it.

Sources, in order:
  1. Direct public URL  /upload/reportOutput/Golf_Competition_Report_<board>.pdf
  2. Authenticated results lookup (needs WWCC_USERNAME / WWCC_PASSWORD) for comps
     whose report is named by a different id (direct URL 404s).

Individual comps only. Team comps (4BBB / ambrose, names with ' & ') are skipped
with a note - their PDF layout pairs names and needs separate handling.

Usage:
    python scripts/rebuild_history_from_pdf.py --all            # dry-run, all broken
    python scripts/rebuild_history_from_pdf.py --all --apply    # write changes
    python scripts/rebuild_history_from_pdf.py --date 2026-08-31 --apply
    python scripts/rebuild_history_from_pdf.py --board 10414329 --apply
"""
import argparse, glob, json, os, re, sys, urllib.request, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from miscore.live import _parse_pdf_standings

BASE = "https://wwcc.com.au"
HIST = pathlib.Path("out/history")
UA   = "Mozilla/5.0 (compatible; WWCC-Kiosk/1.0)"


def normkey(name):
    """Order- and punctuation-independent name key so 'Andrew Grace' (board) and
    'Grace Andrew' (PDF, Lastname Firstname) match."""
    n = re.sub(r"\s*\[[^\]]*\]", "", name or "")          # strip [hcp]
    toks = [t for t in re.split(r"[\s,]+", n.lower()) if t]
    return " ".join(sorted(toks))


def _surnames_blob(name):
    """Team member surnames from a board team name 'First Last [hcp] & First Last...'."""
    out = set()
    for m in re.split(r"\s*&\s*", re.sub(r"\s*\[[^\]]*\]", "", name or "")):
        toks = m.strip().split()
        if len(toks) >= 2:
            out.add(toks[-1].lower())
    return out


def _surnames_pdf(name):
    """Team member surnames from a PDF team row 'Lastname Firstname & Lastname...'
    (competition report lists Lastname first; the last member is often truncated)."""
    out = set()
    for m in re.split(r"\s*&\s*", name or ""):
        toks = [t for t in re.split(r"[\s,]+", m.strip()) if t and t != "-"]
        if toks:
            out.add(toks[0].lower())
    return out


def flip_name(name):
    """PDF 'Lastname Firstname' -> 'Firstname Lastname' for display."""
    if "," in name:
        a, b = name.split(",", 1)
        return f"{b.strip()} {a.strip()}"
    parts = name.split()
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    if len(parts) > 2:
        return f"{parts[-1]} {' '.join(parts[:-1])}"
    return name


def fetch_local(date, board_id):
    """A manually-uploaded PDF placed at out/history/pdf/<date>-<board>.pdf takes
    priority - this is how comps whose report has aged out of MiClub's index get
    recovered: drop the PDF in and re-run."""
    p = HIST / "pdf" / f"{date}-{board_id}.pdf"
    if p.exists():
        b = p.read_bytes()
        if b[:4] == b"%PDF":
            print(f"      using uploaded PDF {p}")
            return b
    return None


def fetch_direct(board_id):
    url = f"{BASE}/upload/reportOutput/Golf_Competition_Report_{board_id}.pdf"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            if r.status == 200:
                b = r.read()
                if b[:4] == b"%PDF":
                    return b
    except Exception:
        pass
    return None


def fetch_api(comp_title, comp_date, blob_keys):
    """Fallback via the public WWCC results API for comps whose report is named by
    a different id (direct URL 404s). The API matches by title+date and CAN return
    the wrong comp, so only accept a PDF whose player names strongly overlap this
    blob's field (>=75% of the PDF's names must be in the blob) - that rejects a
    fuzzy match onto a different date's comp."""
    try:
        from miscore.live import _wwcc_check_results, _wwcc_get_bytes
        res = _wwcc_check_results(comp_title, comp_date)
        best = None
        for link in (res.get("reportLinks") or []):
            try:
                b = _wwcc_get_bytes(link if link.startswith("http") else BASE + link)
                if not (b and b[:4] == b"%PDF"):
                    continue
                rows = (_parse_pdf_standings(b) or {}).get("players") or []
                if not rows:
                    continue
                pk = [normkey(r.get("name")) for r in rows if normkey(r.get("name"))]
                if not pk:
                    continue
                overlap = sum(1 for k in pk if k in blob_keys) / len(pk)
                if overlap >= 0.75 and (best is None or len(rows) > best[1]):
                    best = (b, len(rows), overlap)
                else:
                    print(f"      api PDF rejected: {link[-45:]} overlap {overlap:.0%} ({len(rows)} rows)")
            except Exception:
                continue
        if best:
            print(f"      api PDF accepted: {best[1]} rows, overlap {best[2]:.0%}")
            return best[0]
    except Exception as e:
        print(f"      api lookup failed: {e}")
    return None


def score_val(s, is_stableford):
    try:
        v = float(str(s).strip())
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v


def rebuild(path, apply):
    d = json.load(open(path, encoding="utf-8"))
    board_id = os.path.basename(path).replace(".json", "").split("-")[-1]
    comp = d.get("competition") or ""
    players = d.get("players") or []
    is_team = any(" & " in (p.get("player") or "") for p in players[:5])
    tag = f"{d.get('date')}  {comp[:34]:34s} [{board_id}]"

    blob_keys = {normkey(p.get("player")) for p in players if normkey(p.get("player"))}
    pdf = (fetch_local(d.get("date"), board_id)
           or fetch_direct(board_id)
           or fetch_api(comp, d.get("date"), blob_keys))
    if not pdf:
        print(f"  NOPDF {tag}  (direct 404 and no authed result)")
        return None

    st = _parse_pdf_standings(pdf)
    prows = st.get("players") or []
    if not prows:
        print(f"  EMPTY {tag}  (PDF parsed 0 players)")
        return None

    is_sf = d.get("isStableford", True)
    hc = d.get("holeCount") or 18

    matched, ball_winners = 0, []
    if is_team:
        # Team comps (Irish / 4BBB / ambrose): match a board team to a PDF team row
        # by surname set - the report lists 'Lastname Firstname' and often truncates
        # the last member, so accept when the PDF row's surnames are a subset of the
        # board team's surnames, and the match is unique (no ambiguity).
        prow_sn = [(_surnames_pdf(r.get("name")), r) for r in prows]
        prow_sn = [(sn, r) for sn, r in prow_sn if sn]
        for p in players:
            bs = _surnames_blob(p.get("player"))
            if not bs:
                continue
            cands = [r for sn, r in prow_sn if sn <= bs]
            if len(cands) != 1:
                continue
            sv = score_val(cands[0].get("score"), is_sf)
            if sv is None:
                continue
            p["points"] = sv
            p["thru"] = hc
            matched += 1
    else:
        # Individual comps: order-independent name key.
        by_key = {}
        for r in prows:
            k = normkey(r.get("name"))
            if k and k not in by_key:
                by_key[k] = r
        for p in players:
            r = by_key.get(normkey(p.get("player")))
            if not r:
                continue
            sv = score_val(r.get("score"), is_sf)
            if sv is None:
                continue
            p["points"] = sv
            p["thru"] = hc
            matched += 1
            b = str(r.get("balls") or "").strip()
            if b and b not in ("0", "-"):
                ball_winners.append(p["player"])

    # pdfStandings with names flipped to First Last for the results page's grade lookup
    std_players = [{
        "pos": r.get("pos"), "name": flip_name(r.get("name") or ""),
        "grade": r.get("grade", ""), "score": r.get("score"), "balls": r.get("balls", ""),
    } for r in prows]

    d["pdfStandings"] = {"grades": st.get("grades", []), "players": std_players}
    d["ballWinners"] = sorted(set(ball_winners))
    d["officialResultsReady"] = True
    d["started"] = True

    # refresh leaders (top by score) for the index
    scored = [p for p in players if (p.get("points") or 0) or p.get("thru")]
    scored.sort(key=lambda p: (p.get("points") or 0), reverse=is_sf)
    d["leaders"] = [{k: v for k, v in p.items() if k not in ("holes", "last")}
                    for p in scored[:3]]

    print(f"  OK    {tag}  matched {matched}/{len(players)}  balls {len(d['ballWinners'])}"
          + ("" if matched >= len(players) * 0.6 else "  <-- LOW MATCH, review"))
    if apply:
        json.dump(d, open(path, "w", encoding="utf-8"), separators=(",", ":"))
        # keep a permanent copy of the source PDF
        pdfdir = HIST / "pdf"; pdfdir.mkdir(parents=True, exist_ok=True)
        dest = pdfdir / f"{d.get('date')}-{board_id}.pdf"
        if not dest.exists():
            dest.write_bytes(pdf)
    return {"board_id": board_id, "leader": (scored[0]["player"] if scored else ""),
            "leaderPts": (scored[0].get("points") if scored else 0)}


def update_index(results):
    ip = HIST / "index.json"
    idx = json.load(open(ip, encoding="utf-8"))
    by_id = {r["board_id"]: r for r in results if r}
    for e in idx:
        r = by_id.get(str(e.get("leaderboardId")))
        if r:
            e["leader"], e["leaderPts"] = r["leader"], r["leaderPts"]
    json.dump(idx, open(ip, "w", encoding="utf-8"), indent=2)
    print(f"index.json updated for {len(by_id)} comps")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--board")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="reprocess even blobs that already have points+pdfStandings "
                         "(use to REPLACE a wrong/mismatched PDF once the date-gated "
                         "lookup can fetch the correct one). Only sensible with --board/--date.")
    args = ap.parse_args()

    files = sorted(f for f in glob.glob(str(HIST / "*.json")) if "index" not in f)
    if args.board:
        files = [f for f in files if f.endswith(f"-{args.board}.json")]
    elif args.date:
        files = [f for f in files if os.path.basename(f).startswith(args.date)]
    elif not args.all:
        ap.error("pass --all, --date YYYY-MM-DD, or --board ID")

    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: scanning {len(files)} blob(s)\n")
    results = []
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        ps = d.get("players") or []
        # only touch blobs that look broken: no player has points, or no pdf standings
        # (--force overrides this so a wrong PDF can be replaced with the correct one)
        p_pos = sum(1 for p in ps if (p.get("points") or 0) > 0)
        has_pdf = bool((d.get("pdfStandings") or {}).get("players"))
        if ps and (args.force or p_pos == 0 or not has_pdf):
            r = rebuild(f, args.apply)
            if r:
                results.append(r)
    if args.apply and results:
        update_index(results)
    print(f"\nDone. {'wrote' if args.apply else 'would fix'} {len(results)} comp(s).")


if __name__ == "__main__":
    main()
