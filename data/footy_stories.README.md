# Weekly footy scoreboard stories (data/footy_stories.json)

This file drives the AFL/NRL "footy weekend" flavour stories that scroll on the TV
leaderboard. `miscore/live.py` (`_footy_week_story`) reads it every poll and, while
today is inside the active window, ties a few golfers' rounds to the weekend's footy.

It is regenerated automatically every **Monday morning** by a scheduled Claude Code
routine (see "The Monday routine" below). No prompting needed.

## Schema

```json
{
  "activeFrom": "YYYY-MM-DD",     // first day these show (inclusive) - the Monday
  "activeTo":   "YYYY-MM-DD",     // last day these show (inclusive) - the Sunday
  "generatedAt": "ISO8601",       // when the pack was written
  "cardTitle": "Footy Weekend",   // the story card heading (e.g. "Grand Final", "Round 24")
  "note": "one line describing the weekend's results for the record",
  "phrases": [
    "{player} ...",               // each MUST contain the literal {player} placeholder
    "..."
  ]
}
```

- `phrases` is a flat pool. The engine features the leader, a mid-field golfer and
  the runner-up on any given board, picking distinct phrases (stable per day).
- Every phrase must include `{player}` exactly once and read naturally when a golfer's
  name is substituted in. Keep them short (fits a TV card), upbeat, and clean.
- 15-25 phrases is a good pack.

## Rules (match the rest of the site)

- **No em dashes or en dashes anywhere** - plain hyphen only.
- Reference the real weekend result in the wording (winners, margins, big moments,
  ladder/finals context) so it feels current - but keep each line about the golfer,
  not a bare score readout.
- Out of season (roughly mid-Oct to Feb) or a quiet week: set `phrases` to `[]` (or an
  empty window) so nothing footy shows. The engine already no-ops on an empty pack.

## The Monday routine

A scheduled routine runs every Monday ~7am Sydney time and:
1. Web-searches the weekend's AFL and NRL results (scores, margins, standout moments;
   in Sept/Oct also finals/Grand Final context).
2. If footy was on, writes a fresh pack here with `activeFrom` = that Monday and
   `activeTo` = the coming Sunday, then self-tests, commits and pushes.
3. If there were no games (off-season/bye), writes an empty `phrases` pack so the TV
   shows no stale footy lines.

To refresh manually at any time, just ask Claude to "update this week's footy stories".
