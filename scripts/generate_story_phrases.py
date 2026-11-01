#!/usr/bin/env python3
"""Monthly rebuild of the TV leaderboard phrase bank (data/story_phrases.json).

Runs on the 1st of each month via GitHub Actions. Uses Claude (same setup as the
weekly footy updater) to write a FRESH batch of scoreboard-story lines across all
groups:

  * the men's / general banter bank (season-aware AFL/NRL via web search), and
  * a softer, encouraging women's variant for every relevant category, stored
    under "<cat>_w" keys. miscore/live.py switches to these on ladies' comps
    (see _is_womens_comp / _SOFT_WOMENS).

Safety: the CURRENT bank is the baseline. A category is only replaced when the
freshly generated list passes validation (placeholders a subset of what the
engine passes, renders cleanly, no em dashes, enough lines). Anything that fails
keeps the existing lines, so the board never ends up thin or broken. Blue-ball
(bb_*) categories are men's-only (Wed/Sat) and get no women's variant.

Env:
  ANTHROPIC_API_KEY   - required (GitHub secret)
  STORY_MODEL         - optional model override (default claude-opus-5)

Usage:
  python scripts/generate_story_phrases.py            # full rebuild (all groups)
  python scripts/generate_story_phrases.py --only weather w_weather
  python scripts/generate_story_phrases.py --check    # validate current file, no API
"""
import argparse
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "story_phrases.json"
MODEL = os.environ.get("STORY_MODEL") or "claude-opus-5"

MIN_KEEP = 6          # a generated category needs at least this many valid lines to be used
TOKEN_RE = re.compile(r"\{(\w+)\}")

# Sample values for every placeholder the engine uses, so a line can be test-rendered.
SAMPLE = {
    "player": "Sample Golfer", "team": "The Sample Team", "score": "40 pts",
    "wind": "45", "rain": "12", "tmax": "34", "climbed": "5",
    "best_ord": "2nd", "rank_ord": "9th", "avg": "31", "who": "Jo and Sam",
    "sn": "Sample", "n": "3", "club": "Visiting GC", "winner": "Sample",
    "loser": "Other", "wp": "19", "lp": "17", "mavg": "4.1", "aavg": "4.4",
    "gross": "68", "net": "64", "margin": "3", "second": "Other Team",
    "other": "Other Team", "hcp": "21", "pts": "28", "need": "5", "left": "3",
    "above": "Sample", "cushion": "4", "leader": "Sample", "wx": "heat",
}


def clean(text):
    """Site rule: no em/en dashes anywhere - plain hyphen only."""
    return (text or "").replace("—", "-").replace("–", "-").strip()


# ── The spec: groups of categories to (re)generate, with intent + target count ──
# tone: "banter" (men's/general) or "soft" (women's, encouraging). websearch adds
# live sport context. For a "soft" group the keys are the real bank keys (already
# suffixed _w); placeholders are derived from the matching base category.
GROUPS = [
    {"name": "weather", "tone": "banter", "websearch": False, "cats": {
        "wx_wind": ("The conditions line when it was windy; wry, about the whole field.", 20),
        "wx_rain": ("The conditions line when it rained; grinding-it-out tone.", 20),
        "wx_heat": ("The conditions line on a hot day.", 18),
        "wx_cold": ("The conditions line on a cold day.", 18),
        "hero_wind": ("Praise for a player scoring well in strong wind.", 22),
        "hero_rain": ("Praise for a player scoring well in the rain.", 20),
        "hero_heat": ("Praise for a player scoring well in the heat.", 18),
        "hero_cold": ("Praise for a player scoring well in the cold.", 18),
        "calm_day": ("A player scoring well on a perfect, no-excuses day.", 18),
    }},
    {"name": "footy_core", "tone": "banter", "websearch": True, "cats": {
        "footy_leader": ("The comp leader, with an AFL/NRL ladder-leader analogy.", 26),
        "footy_charge": ("A player who jumped {climbed} spots, like a fast-finishing side.", 24),
        "footy_slump": ("A player who slid from {best_ord} to {rank_ord}; gentle footy-fade analogy.", 22),
        "footy_spoon": ("The player at the bottom, with a wooden-spoon footy analogy.", 22),
        "footy_rough": ("A player home on a rough score, footy analogy for a bad day.", 22),
        "footy_noshow": ("A player who did not fire, footy analogy for a no-show.", 20),
    }},
    {"name": "footy_finals", "tone": "banter", "websearch": True, "cats": {
        "footy_finals_leader": ("Leader peaking at finals time.", 14),
        "footy_finals_fade": ("A player fading like a side that bombed a final.", 14),
        "footy_finals_bolter": ("A charging underdog, September bolter.", 13),
        "footy_finals_spoon": ("Bottom player while the good sides play finals.", 12),
        "footy_finals_general": ("General finals-fever flavour about a player.", 14),
        "movie_leader": ("Leader, upbeat sports-movie-quote flavour.", 18),
        "movie_rough": ("A struggling player, light sports-movie flavour.", 14),
        "movie_general": ("General sports-movie flavour about a player.", 16),
        "movie_spoon": ("Bottom player, playful sports-movie flavour.", 12),
    }},
    {"name": "blueball", "tone": "banter", "websearch": False, "cats": {
        "bb_holder_fin": ("Blue Ball roast - the holder has finished lowest (men's Wed/Sat only).", 40),
        "bb_holder_out": ("Blue Ball - holder needs {need} from {left} to hand it to {above}.", 30),
        "bb_contender": ("Blue Ball - {cushion} clear of the blue balls over {leader}.", 28),
        "bb_contender_fin": ("Blue Ball - finished {cushion} above {leader}.", 20),
        "bb_tied": ("Blue Ball - level with {leader} at the bottom.", 12),
        "bb_lowmarker": ("Blue Ball - a low marker off {hcp} staring at the double.", 24),
        "bb_highmarker": ("Blue Ball - a high marker off {hcp} battling.", 22),
        "bb_weather": ("Blue Ball - at least they can blame the {wx}.", 20),
        "bb_locked": ("Blue Ball - on {pts}, nearly locked in.", 20),
    }},
    {"name": "social", "tone": "banter", "websearch": False, "cats": {
        "family_affair": ("Two family members ({who}, surname {sn}) both on the board.", 28),
        "family_affair_three": ("{n} of the {sn} family out together.", 18),
        "visitor_leading": ("A visitor from {club} leading the locals.", 22),
        "visitor_welcome": ("Warm welcome to visitor {player} from {club}.", 12),
        "career_day": ("A player's career-best round on {score}.", 20),
        "great_escape": ("A player who just dodged the bottom on {score}.", 18),
        "group_rivalry": ("{winner} beat {loser} {wp} to {lp} in their group.", 18),
        "group_rivalry_tight": ("{winner} just pipped {loser} {wp} to {lp}.", 14),
        "wave_morning": ("The morning wave outscored the afternoon ({mavg} vs {aavg}).", 12),
        "wave_afternoon": ("The afternoon wave outscored the morning ({aavg} vs {mavg}).", 12),
    }},
    {"name": "team", "tone": "banter", "websearch": False, "cats": {
        "team_leader": ("Team out in front on {score}.", 20),
        "team_leader_tight": ("Team leading on {score} by {margin} over {second}.", 18),
        "team_leader_clear": ("Team clear on {score}, {margin} ahead.", 18),
        "team_family": ("A family pair ({sn}) teaming up.", 18),
        "team_lowhcp": ("Team off a low combined {hcp} on {score}.", 18),
        "team_highhcp": ("Team off a big combined {hcp}, shots paying off on {score}.", 18),
        "team_countback": ("Team locked with {other} on {score}, countback looms.", 16),
        "team_midpack": ("Team sitting comfortably mid-pack on {score}.", 18),
        "team_backmarker": ("Team near the bottom, enjoying themselves.", 18),
        "team_flavour": ("General two-players-one-card flavour.", 20),
    }},
    {"name": "ambrose", "tone": "banter", "websearch": False, "cats": {
        "ambrose_leader": ("Ambrose leaders, gross {gross} for net {net}.", 22),
        "ambrose_leader_clear": ("Ambrose leaders clear by {margin}, gross {gross}.", 16),
        "ambrose_leader_tight": ("Ambrose leaders by {margin} over {second}.", 14),
        "ambrose_family": ("A family-surname ({sn}) ambrose team, gross {gross}.", 20),
        "ambrose_lowhcp": ("Ambrose team off a low combined {hcp}, gross {gross}.", 18),
        "ambrose_highhcp": ("Ambrose team off a big combined {hcp}, net {net}.", 18),
        "ambrose_midpack": ("Ambrose team mid-field on net {net}.", 18),
        "ambrose_backmarker": ("Ambrose team at the back, gross {gross}, having fun.", 18),
        "ambrose_countback": ("Ambrose team locked with {other} on net {net}.", 14),
        "ambrose_flavour": ("General ambrose two-heads flavour, gross {gross}.", 22),
    }},
    # ── Women's soft variants (encouraging, warm, positive; no roasting) ──
    {"name": "w_weather", "tone": "soft", "websearch": False, "cats": {
        "wx_wind_w": ("Gentle note on a windy day for the ladies' comp.", 12),
        "wx_rain_w": ("Gentle, encouraging note on a wet day.", 12),
        "wx_heat_w": ("Warm, caring note on a hot day (hydrate, well done).", 12),
        "wx_cold_w": ("Warm note on a cold day, rugged up and smiling.", 12),
        "hero_wind_w": ("Celebrate a player scoring well in the wind, kindly.", 12),
        "hero_rain_w": ("Celebrate a player scoring well in the rain, kindly.", 12),
        "hero_heat_w": ("Celebrate a player scoring well in the heat, kindly.", 12),
        "hero_cold_w": ("Celebrate a player scoring well in the cold, kindly.", 12),
        "calm_day_w": ("A player enjoying a lovely day and a good score.", 12),
    }},
    {"name": "w_footy", "tone": "soft", "websearch": False, "cats": {
        "footy_leader_w": ("Warmly celebrate the leader; light, positive, no sting.", 14),
        "footy_charge_w": ("Encourage a player climbing {climbed} spots; proud of the run.", 14),
        "footy_slump_w": ("Kindly reassure a player who slipped from {best_ord} to {rank_ord}; plenty of golf left.", 14),
        "footy_spoon_w": ("Gently uplift the player at the bottom; every round counts, keep smiling.", 14),
        "footy_rough_w": ("Kindly support a player after a tough score; tomorrow is brighter.", 14),
        "footy_noshow_w": ("Gently encourage a player whose day did not click; it happens to everyone.", 12),
        "footy_finals_leader_w": ("Warmly celebrate a front-runner, gentle finals flavour.", 10),
        "footy_finals_fade_w": ("Kindly reassure a player who faded late.", 10),
        "footy_finals_bolter_w": ("Cheer a lovely late charge.", 10),
        "footy_finals_spoon_w": ("Gently uplift the player at the bottom.", 10),
        "footy_finals_general_w": ("General warm, encouraging flavour about a player.", 12),
    }},
    {"name": "w_social", "tone": "soft", "websearch": False, "cats": {
        "family_affair_w": ("Lovely note on two family ({who}, {sn}) playing together.", 16),
        "family_affair_three_w": ("Warm note on {n} of the {sn} family out together.", 12),
        "visitor_leading_w": ("Gracious welcome to a visitor from {club} going well.", 12),
        "visitor_welcome_w": ("Warm welcome to visitor {player} from {club}.", 10),
        "career_day_w": ("Joyfully celebrate a career-best round on {score}.", 14),
        "great_escape_w": ("Warmly praise a player who held on well on {score}.", 12),
        "group_rivalry_w": ("Friendly note on {winner} and {loser} enjoying their group.", 12),
        "group_rivalry_tight_w": ("Friendly note on a close, fun group finish.", 10),
        "wave_morning_w": ("Nice note on the morning group scoring well.", 10),
        "wave_afternoon_w": ("Nice note on the afternoon group scoring well.", 10),
    }},
    {"name": "w_team", "tone": "soft", "websearch": False, "cats": {
        "team_leader_w": ("Warmly celebrate a team in front on {score}.", 12),
        "team_leader_tight_w": ("Encourage a team leading on {score} by {margin}.", 10),
        "team_leader_clear_w": ("Warmly celebrate a team clear on {score}.", 10),
        "team_family_w": ("Lovely note on a family pair ({sn}).", 12),
        "team_lowhcp_w": ("Kind note on a team off a low {hcp} on {score}.", 10),
        "team_highhcp_w": ("Encourage a team off a big {hcp} making it count on {score}.", 10),
        "team_countback_w": ("Friendly note on a team tied with {other} on {score}.", 10),
        "team_midpack_w": ("Warm note on a team enjoying the day mid-pack on {score}.", 12),
        "team_backmarker_w": ("Gently uplift a team near the back, having fun.", 12),
        "team_flavour_w": ("General warm two-together flavour.", 14),
    }},
    {"name": "w_ambrose", "tone": "soft", "websearch": False, "cats": {
        "ambrose_leader_w": ("Warmly celebrate ambrose leaders, gross {gross} net {net}.", 12),
        "ambrose_leader_clear_w": ("Celebrate ambrose leaders clear by {margin}.", 10),
        "ambrose_leader_tight_w": ("Encourage ambrose leaders by {margin} over {second}.", 10),
        "ambrose_family_w": ("Lovely note on a family ({sn}) ambrose team.", 12),
        "ambrose_lowhcp_w": ("Kind note on a team off a low {hcp}, gross {gross}.", 10),
        "ambrose_highhcp_w": ("Encourage a team off a big {hcp}, net {net}.", 10),
        "ambrose_midpack_w": ("Warm note on an ambrose team mid-field, net {net}.", 12),
        "ambrose_backmarker_w": ("Gently uplift an ambrose team at the back, having fun.", 12),
        "ambrose_countback_w": ("Friendly note on a team tied with {other} on net {net}.", 10),
        "ambrose_flavour_w": ("General warm ambrose togetherness flavour.", 14),
    }},
]


def allowed_tokens(bank, cat):
    """Placeholders the engine passes for a category = those seen in its current
    lines. A women's `_w` category inherits the base category's set."""
    base = cat[:-2] if cat.endswith("_w") else cat
    toks = set()
    for line in bank.get(base) or bank.get(cat) or []:
        if isinstance(line, str):
            toks |= set(TOKEN_RE.findall(line))
    return toks


def required_token(allowed):
    if "player" in allowed:
        return "player"
    if "team" in allowed:
        return "team"
    return None


def valid_line(line, allowed, req):
    if not isinstance(line, str):
        return None
    s = clean(line)
    if not s or len(s) > 170:
        return None
    used = set(TOKEN_RE.findall(s))
    if not used.issubset(allowed):            # never reference a field the engine won't pass
        return None
    if req and s.count("{" + req + "}") < 1:   # must be about the golfer / the team
        return None
    try:
        s.format(**{k: SAMPLE.get(k, "x") for k in allowed})
    except Exception:
        return None
    return s


def validate_list(raw, allowed, req):
    out, seen = [], set()
    for line in raw or []:
        s = valid_line(line, allowed, req)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def ask_group(group, bank):
    """Generate one group -> {cat: [lines]} (only categories that pass)."""
    import anthropic
    client = anthropic.Anthropic()
    soft = group["tone"] == "soft"
    tone_rule = (
        "TONE: warm, positive, encouraging, supportive. This is the ladies' "
        "midweek competition - celebrate effort and enjoyment, never roast, "
        "mock, or sting. Gentle good humour is welcome; put-downs are not."
        if soft else
        "TONE: cheeky, affectionate club banter - the good-natured ribbing of a "
        "country golf club. Confident and fun, never cruel."
    )
    specs = []
    for cat, (intent, count) in group["cats"].items():
        allowed = sorted(allowed_tokens(bank, cat))
        specs.append({"category": cat, "count": count,
                      "placeholders": ["{" + a + "}" for a in allowed],
                      "intent": intent})
    footy = (
        "This group references real AFL and NRL. Use web search to ground it in "
        "the most recent season (winners, finals, big names) and today's date. Do "
        "NOT pin every line to one round or one final - keep it varied and current "
        "so it never looks stuck. If deep in the off-season, lean on the season "
        "just gone and timeless footy culture. Keep it light."
        if group["websearch"] else ""
    )
    system = (
        "You write one-line scoreboard-story captions for the TV leaderboard at "
        "Wagga Wagga Country Club, a golf club. Each line flashes on a TV card "
        "beside a player's name. ABSOLUTE RULES: (1) no em dashes or en dashes "
        "anywhere - plain hyphen only; (2) use ONLY the placeholders listed for a "
        "category, each written exactly like {player}; (3) if {player} is listed, "
        "use it exactly once; if {team} is listed (and no player), use it; (4) one "
        "sentence, under ~160 characters, no leading dash or bullet; (5) Australian "
        "spelling and idiom. Return ONLY a JSON object."
    )
    user = f"""Write fresh scoreboard lines for these categories.

{tone_rule}
{footy}

Return ONLY a JSON object in a ```json fenced block, mapping each category name to
an array of that many DISTINCT lines:

{json.dumps(specs, indent=1)}

Every line must read naturally once the placeholders are filled (e.g. {{player}} ->
a golfer's name, {{score}} -> "40 pts"). Do not add categories that are not listed."""

    tools = []
    if group["websearch"]:
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6,
                  "user_location": {"type": "approximate", "country": "AU",
                                    "timezone": "Australia/Sydney"}}]
    messages = [{"role": "user", "content": user}]
    data = None
    for _ in range(6):
        resp = client.messages.create(model=MODEL, max_tokens=16000, system=system,
                                      tools=tools, messages=messages)
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model refused group {group['name']}")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = _extract_json(text)
        break
    if data is None:
        raise RuntimeError(f"group {group['name']} did not settle")

    result = {}
    for cat in group["cats"]:
        allowed = allowed_tokens(bank, cat)
        req = required_token(allowed)
        lines = validate_list(data.get(cat), allowed, req)
        if len(lines) >= MIN_KEEP:
            result[cat] = lines
            print(f"  {cat}: {len(lines)} lines OK")
        else:
            print(f"  {cat}: only {len(lines)} valid - KEEPING existing")
    return result


def _extract_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError("no JSON object in model output")


def check_file():
    """Validate the current file without the API (used by the workflow)."""
    bank = json.loads(OUT.read_text(encoding="utf-8"))
    problems, total = [], 0
    for cat, lines in bank.items():
        if cat == "_readme" or not isinstance(lines, list):
            continue
        allowed = allowed_tokens(bank, cat)
        req = required_token(allowed)
        for ln in lines:
            total += 1
            if "—" in ln or "–" in ln:
                problems.append(f"{cat}: em/en dash in {ln!r}")
            if valid_line(ln, allowed, req) is None and req:
                problems.append(f"{cat}: unrenderable {ln!r}")
    print(f"Checked {total} lines across the bank")
    for p in problems[:20]:
        print("  PROBLEM:", p)
    return 0 if not problems else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="generate only these group names")
    ap.add_argument("--check", action="store_true", help="validate current file, no API")
    args = ap.parse_args()

    if args.check:
        return check_file()

    bank = json.loads(OUT.read_text(encoding="utf-8"))   # baseline / fallback
    groups = [g for g in GROUPS if not args.only or g["name"] in args.only]
    replaced = 0
    for g in groups:
        print(f"[group] {g['name']} ({g['tone']})")
        try:
            got = ask_group(g, bank)
        except Exception as e:
            print(f"  group {g['name']} FAILED ({e}) - keeping all its existing lines")
            continue
        for cat, lines in got.items():
            bank[cat] = lines
            replaced += 1

    OUT.write_text(json.dumps(bank, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    womens = sum(1 for k in bank if k.endswith("_w"))
    print(f"Wrote {OUT}: {replaced} categories refreshed, {womens} women's (_w) categories present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
