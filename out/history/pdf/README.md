# Competition results PDFs

Permanent copies of each comp's official Golf Competition Report PDF - the single
source of truth for Past Results. From now on the live poller saves these here
automatically the moment results are published, and rebuilds the Past Results
board from them (`scripts/rebuild_history_from_pdf.py`).

## Uploading a missing PDF (for old comps whose report has aged out of MiClub)

1. Download the comp's **Golf Competition Report** PDF from MiClub.
2. Save it here named exactly: **`<date>-<board>.pdf`** (e.g. `2026-09-02-10414239.pdf`).
   The `<board>` id is the number in that comp's history filename
   (`out/history/2026-09-02-10414239.json`).
3. Run: `python scripts/rebuild_history_from_pdf.py --date 2026-09-02 --apply`
   (an uploaded PDF here takes priority over any download attempt).

## Comps still needing a manual PDF upload (as of 21 Sep 2026)

Individual comps currently showing **0 points** (can't be auto-fetched - MiClub's
report is named by a different id and its results index only goes back ~1 week):

| Date | Comp | Save as |
|------|------|---------|
| 2026-09-02 | Mens Wednesday Stableford | `2026-09-02-10414239.pdf` |
| 2026-09-08 | Ladies 18 Hole Stableford | `2026-09-08-10414348.pdf` |
| 2026-09-08 | Ladies 9 Hole Stableford Front 9 | `2026-09-08-10414349.pdf` |
| 2026-09-09 | Mens Wednesday Stableford | `2026-09-09-10414242.pdf` |
| 2026-09-19 | Saturday Medley Stableford | `2026-09-19-10414289.pdf` |
| 2026-09-21 | Veterans 9 Hole Stableford Front 9 | `2026-09-21-10414368.pdf` |

Individual comps showing live points but not PDF-verified (upload optional):

| Date | Comp | Save as |
|------|------|---------|
| 2026-09-10 | 2026 PSC Insurance Brokers Pro Am | `2026-09-10-10414357.pdf` |
| 2026-09-10 | 2026 Brett Bischard Trophy | `2026-09-10-10414358.pdf` |

Comps whose WRONG PDF was cleared (had another comp's prizes attached; correct
report can't be auto-fetched - upload it to restore ball/grade winners):

| Date | Comp | Save as |
|------|------|---------|
| 2026-09-11 | PSC Pro Am Rd 2 (had Rd 1's PDF) | `2026-09-11-10414351.pdf` |

Team comps (4BBB / ambrose) - show live points; uploading preserves the source
PDF, but the rebuild tool doesn't yet re-score teams from the PDF (individual
matching only), so the board keeps its live points for now:

| Date | Comp | Save as |
|------|------|---------|
| 2026-08-19 | Wednesday 4BBB Stableford | `2026-08-19-10414235.pdf` |
| 2026-08-22 | Saturday 4BBB Stableford | `2026-08-22-10414276.pdf` |
| 2026-08-26 | Wednesday 4BBB Stableford | `2026-08-26-10414238.pdf` |
| 2026-08-29 | Saturday 4BBB Stableford | `2026-08-29-10414279.pdf` |
| 2026-09-12 | Saturday 4BBB Stableford | `2026-09-12-10414285.pdf` |
| 2026-09-13 | GolfNSW 2 Person Ambrose | `2026-09-13-10414359.pdf` |
| 2026-09-19 | Irish Team Stableford | `2026-09-19-10414365.pdf` |
