import json
import logging
from typing import Optional
import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

_ANALYST_SYSTEM = (
    "You are an expert sports betting analyst with deep knowledge across NBA, NHL, MLB, NFL, "
    "UFC/MMA, NCAAB, and NCAAF. You analyze odds, line movement, matchup trends, and situational "
    "factors to identify high-value betting opportunities. You are sharp, data-driven, and concise. "
    "You prioritize expected value over gut feeling."
)


# ── Odds formatting (shared between picks generator and bot) ──────────────────

def format_odds_for_prompt(all_odds: dict[str, list[dict]], max_chars: int = 6000) -> str:
    lines: list[str] = []
    for sport, games in all_odds.items():
        lines.append(f"\n=== {sport} ===")
        for game in games:
            home = game.get("home_team", "?")
            away = game.get("away_team", "?")
            tip = game.get("commence_time", "")[:16].replace("T", " ")
            lines.append(f"\n{away} @ {home}  [{tip} UTC]")

            seen: set[str] = set()
            for bk in game.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    mkey = mkt["key"]
                    if mkey in seen:
                        continue
                    seen.add(mkey)
                    label = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}.get(mkey, mkey)
                    parts: list[str] = []
                    for o in mkt.get("outcomes", []):
                        try:
                            price = int(o["price"])
                        except (KeyError, ValueError):
                            continue
                        sign = "+" if price > 0 else ""
                        pt = o.get("point")
                        pt_str = f" {'+' if pt and pt > 0 else ''}{pt}" if pt is not None else ""
                        parts.append(f"{o.get('name','?')}{pt_str}: {sign}{price}")
                    if parts:
                        lines.append(f"  {label}: {' | '.join(parts)}")
                break  # one bookmaker per game is enough context

    text = "\n".join(lines)
    return text[:max_chars]


# ── Daily picks ───────────────────────────────────────────────────────────────

_PICKS_PROMPT = """Analyze all of today's available games and odds below. Select the highest-value betting opportunities.

{odds_text}

Return ONLY a JSON object with EXACTLY this structure — no prose, no markdown fences:
{{
  "lock_of_the_day": {{
    "sport": "NBA",
    "teams": "Lakers vs Celtics",
    "pick": "Celtics -4.5",
    "odds": "-110",
    "analysis": "Sentence one. Sentence two. Sentence three.",
    "confidence": 5,
    "units": 4
  }},
  "top_picks": [
    {{
      "sport": "...",
      "teams": "...",
      "pick": "...",
      "odds": "...",
      "analysis": "Sentence one. Sentence two. Sentence three.",
      "confidence": 4,
      "units": 3
    }}
  ],
  "longshots": [
    {{
      "sport": "...",
      "teams": "...",
      "pick": "...",
      "odds": "...",
      "analysis": "Sentence one. Sentence two. Sentence three.",
      "confidence": 2,
      "units": 1
    }}
  ]
}}

Rules:
- top_picks: 5-7 picks curated ACROSS all sports (not per league)
- longshots: 1-2 higher-risk plays at +200 odds or better
- lock_of_the_day: your single highest-confidence pick from any sport
- confidence: integer 1-5 (5 = highest conviction)
- units: integer 1-5 (proportional to confidence)
- odds: American format string e.g. "-110" or "+155"
- analysis: exactly 3 sentences, factual and sharp — no filler
- Only reference games present in the data above
"""


def get_daily_picks(client: anthropic.Anthropic, all_odds: dict) -> Optional[dict]:
    odds_text = format_odds_for_prompt(all_odds, max_chars=8000)
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=2500,
            system=_ANALYST_SYSTEM,
            messages=[{"role": "user", "content": _PICKS_PROMPT.format(odds_text=odds_text)}],
        )
        raw = msg.content[0].text.strip()
        # Strip accidental markdown fences (handles ```json\n...\n``` pattern)
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip("\n")
        data = json.loads(raw)
        # Validate top-level keys exist
        assert "lock_of_the_day" in data
        assert "top_picks" in data
        assert "longshots" in data
        return data
    except (json.JSONDecodeError, AssertionError, KeyError) as e:
        logger.error(f"Failed to parse Claude picks response: {e}")
        return None
    except anthropic.APIError as e:
        logger.error(f"Claude API error (picks): {e}")
        return None


# ── Ad-hoc analysis (slash commands + DMs) ───────────────────────────────────

def analyze_matchup(client: anthropic.Anthropic, query: str, odds_context: str) -> str:
    prompt = (
        f"A member asks: {query}\n\n"
        f"Today's relevant odds:\n{odds_context}\n\n"
        "Respond in plain conversational text only — no JSON, no markdown, no code blocks, no bullet lists. "
        "Write 3-5 sharp sentences covering the key factors, the line, and your take. Be direct and confident."
    )
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=450,
            system=_ANALYST_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error(f"Claude API error (analyze): {e}")
        return "Analysis temporarily unavailable. Please try again in a moment."


def analyze_parlay(client: anthropic.Anthropic, picks_text: str, odds_context: str) -> str:
    prompt = (
        f"Analyze this parlay:\n{picks_text}\n\n"
        f"Today's odds context:\n{odds_context}\n\n"
        "Calculate approximate combined odds, estimate realistic hit probability, and give a sharp "
        "take on whether the parlay makes sense. Keep it to 4-6 sentences."
    )
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=450,
            system=_ANALYST_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error(f"Claude API error (parlay): {e}")
        return "Parlay analysis temporarily unavailable."
