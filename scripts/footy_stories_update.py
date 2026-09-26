#!/usr/bin/env python3
"""Weekly footy scoreboard-story updater (runs every Monday via GitHub Actions).

Uses Claude with web search to look up the weekend's real AFL and NRL results,
then writes data/footy_stories.json - the pack the TV leaderboard reads
(miscore/live.py -> _footy_week_story). See data/footy_stories.README.md.

Dates and validation are enforced HERE in Python (never trust the model for the
active window); the model only supplies the creative content. Off-season or a
quiet week -> an empty pack, so nothing stale shows on the TV.

Env:
  ANTHROPIC_API_KEY  - required (GitHub secret)
  FOOTY_MODEL        - optional model override (default claude-opus-5)
"""
import datetime
import json
import os
import pathlib
import re
import sys

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

import anthropic

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "footy_stories.json"
MODEL = os.environ.get("FOOTY_MODEL", "claude-opus-5")
MAX_PHRASES = 25


def sydney_now():
    if ZoneInfo:
        return datetime.datetime.now(ZoneInfo("Australia/Sydney"))
    # Fallback: fixed AEST if tz db is unavailable (GitHub runners have it).
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=10)))


def clean(text):
    """Enforce the site rule: no em/en dashes anywhere - plain hyphen only."""
    return (text or "").replace("—", "-").replace("–", "-").strip()


def extract_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError("no JSON object found in model output")


def valid_phrases(raw):
    out = []
    for p in raw or []:
        if not isinstance(p, str):
            continue
        s = clean(p)
        if s.count("{player}") != 1:
            continue
        try:
            s.format(player="Test Golfer")   # must render with a name
        except Exception:
            continue
        out.append(s)
        if len(out) >= MAX_PHRASES:
            break
    return out


def write_pack(active_from, active_to, generated_at, card_title, note, phrases):
    pack = {
        "activeFrom": active_from,
        "activeTo": active_to,
        "generatedAt": generated_at,
        "cardTitle": card_title or "Footy",
        "note": clean(note),
        "phrases": phrases,
    }
    OUT.write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
    return pack


def ask_claude(active_from, active_to):
    client = anthropic.Anthropic()
    system = (
        "You write short, upbeat AFL and NRL flavour lines for a golf club's TV "
        "leaderboard. You research the real weekend results with web search, then "
        "return ONLY a JSON object. Absolute rules: no em dashes or en dashes "
        "anywhere (plain hyphen only); keep every line about the golfer, not a bare "
        "score readout; each line must fit a TV card."
    )
    user = f"""Today is the Monday of the week {active_from} to {active_to} (Australia/Sydney).

1. Use web search to find the AFL and NRL results from the weekend just gone
   (Friday to Sunday): final scores, margins, standout performances, upsets. In
   September and October also get finals / Grand Final context (who won, who is
   still alive). Use reliable sources. Do not invent scores.

2. Decide season status. AFL runs roughly March to late September (Grand Final
   late September). NRL runs roughly March to early October (Grand Final first
   Sunday of October). If BOTH codes are out of season (roughly mid-October
   through February) or there were genuinely no games this weekend, set
   "inSeason": false.

3. Return ONLY a JSON object (in a ```json fenced block), no other text:
{{
  "inSeason": true/false,
  "cardTitle": "short card heading, e.g. Footy Weekend, Grand Final, Round 24",
  "note": "one line summarising the weekend's actual results, for the record",
  "phrases": [
    "15 to 25 lines, each containing the literal placeholder {{player}} exactly once,"
    "each referencing THIS weekend's real results (winners, margins, big moments,"
    "ladder/finals context) and reading naturally when a golfer's name replaces {{player}}"
  ]
}}

If "inSeason" is false, still return the object with an empty "phrases" array and a
"note" saying it is the footy off-season."""

    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 8,
        "user_location": {"type": "approximate", "country": "AU",
                          "timezone": "Australia/Sydney"},
    }]

    messages = [{"role": "user", "content": user}]
    # Server-side web search runs inside the call; loop only on pause_turn.
    for _ in range(6):
        resp = client.messages.create(
            model=MODEL, max_tokens=8000, system=system,
            tools=tools, messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason == "refusal":
            raise RuntimeError("model refused the request")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return extract_json(text)
    raise RuntimeError("web search did not settle after repeated pause_turn")


def main():
    now = sydney_now()
    monday = now.date() - datetime.timedelta(days=now.date().weekday())
    sunday = monday + datetime.timedelta(days=6)
    active_from, active_to = monday.isoformat(), sunday.isoformat()
    generated_at = now.replace(microsecond=0).isoformat()

    data = ask_claude(active_from, active_to)
    phrases = valid_phrases(data.get("phrases")) if data.get("inSeason") else []
    card = data.get("cardTitle") if phrases else "Footy"
    note = data.get("note") or ("Footy off-season - no games this week." if not phrases else "")

    pack = write_pack(active_from, active_to, generated_at, card, note, phrases)
    print(f"Wrote {OUT} : {len(pack['phrases'])} phrases, active {active_from}..{active_to}")
    print(f"  cardTitle: {pack['cardTitle']}")
    print(f"  note: {pack['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
