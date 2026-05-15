"""
Picks generator — fetches odds, asks Claude for analysis,
posts results to the Whop app, and logs to CSV.
"""

import logging
import os

import anthropic

from claude_client import get_daily_picks
from history import log_picks
from odds_client import OddsClient
from whop_client import post_all_picks

logger = logging.getLogger(__name__)


# ── Core generation (reused by scheduler AND API endpoint) ───────────────────

def generate_picks_data(
    odds_client: OddsClient,
    claude_client: anthropic.Anthropic,
) -> tuple[dict | None, dict]:
    """
    Fetch odds + ask Claude for picks.
    Returns (picks_data, all_odds). picks_data is None on failure.
    Does NOT post anywhere — callers decide what to do with the result.
    """
    all_odds = odds_client.get_all_odds()
    if not all_odds:
        logger.warning("No active games found across tracked sports")
        return None, {}

    picks_data = get_daily_picks(claude_client, all_odds)
    return picks_data, all_odds


def post_and_log(picks_data: dict) -> None:
    """
    Post all picks to the Whop app and log to CSV with the IDs Whop returns.
    Those IDs are later used to grade picks via PATCH.
    """
    # Post to Whop — get back {teams: whop_id} mapping
    id_map = post_all_picks(picks_data)

    # Build flat list with IDs attached
    all_picks: list[dict] = []

    lock = picks_data.get("lock_of_the_day")
    if lock:
        all_picks.append({
            **lock,
            "pick_type": "lock",
            "whop_id": id_map.get(lock.get("teams", ""), ""),
        })

    for p in picks_data.get("top_picks", []):
        all_picks.append({
            **p,
            "pick_type": "top",
            "whop_id": id_map.get(p.get("teams", ""), ""),
        })

    for p in picks_data.get("longshots", []):
        all_picks.append({
            **p,
            "pick_type": "longshot",
            "whop_id": id_map.get(p.get("teams", ""), ""),
        })

    log_picks(all_picks)
    logger.info(f"Logged {len(all_picks)} picks to CSV")


# ── Scheduled daily run (reads env vars, self-contained) ─────────────────────

def run_daily_picks() -> None:
    logger.info("Daily picks run starting…")

    odds_key = os.getenv("ODDS_API_KEY", "")
    ai_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not odds_key or not ai_key:
        logger.error("Missing ODDS_API_KEY or ANTHROPIC_API_KEY")
        return

    try:
        odds_client = OddsClient(odds_key)
        claude_client = anthropic.Anthropic(api_key=ai_key)

        picks_data, _ = generate_picks_data(odds_client, claude_client)
        if picks_data:
            post_and_log(picks_data)
        else:
            logger.warning("No picks generated — nothing to post")
    except Exception:
        logger.exception("Unexpected error in daily picks run")
