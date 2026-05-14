"""
REST API — called by the Whop app to serve picks, odds, and AI analysis to members.

All endpoints require the header:  X-Api-Secret: <API_SECRET env var>
Optional per-request header:       X-Member-Id: <whop_user_id>  (enables per-member rate limiting)
"""

import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import anthropic
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from claude_client import analyze_matchup, analyze_parlay, format_odds_for_prompt
from history import get_all_stats, get_weekly_stats
from odds_client import OddsClient
from picks_generator import generate_picks_data, post_and_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# ── Shared clients (set once at startup via set_clients) ─────────────────────

_odds_client: OddsClient | None = None
_claude_client: anthropic.Anthropic | None = None


def set_clients(odds: OddsClient, claude: anthropic.Anthropic) -> None:
    global _odds_client, _claude_client
    _odds_client = odds
    _claude_client = claude


# ── Odds cache (30 min TTL — avoids hammering the free-tier quota) ───────────

_odds_cache: dict = {}
_odds_cache_ts: float = 0.0
_ODDS_TTL = 1800


async def _get_odds() -> dict:
    global _odds_cache, _odds_cache_ts
    now = datetime.now(timezone.utc).timestamp()
    if now - _odds_cache_ts > _ODDS_TTL:
        logger.info("Refreshing odds cache…")
        _odds_cache = await asyncio.to_thread(_odds_client.get_all_odds)
        _odds_cache_ts = now
    return _odds_cache


# ── Latest picks cache (set after each successful generation) ─────────────────

_latest_picks: dict | None = None
_latest_picks_ts: str | None = None
_generation_lock = asyncio.Lock()
_picks_cooldown_ts: float = 0.0
_PICKS_COOLDOWN = 3600  # 1 hour between on-demand generations


# ── Per-member rate limiting for AI endpoints ─────────────────────────────────

_rate_log: dict[str, list[float]] = defaultdict(list)
_RATE_MAX = 5
_RATE_WINDOW = 600  # 10 minutes


def _check_rate(member_id: str) -> None:
    """Raises 429 if member has exceeded their quota."""
    now = datetime.now(timezone.utc).timestamp()
    _rate_log[member_id] = [t for t in _rate_log[member_id] if now - t < _RATE_WINDOW]
    if len(_rate_log[member_id]) >= _RATE_MAX:
        oldest = min(_rate_log[member_id])
        wait = int(_RATE_WINDOW - (now - oldest))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Try again in {wait // 60}m {wait % 60}s.",
        )
    _rate_log[member_id].append(now)


# ── Auth dependency ───────────────────────────────────────────────────────────

def _require_secret(x_api_secret: str = Header("")) -> None:
    expected = os.getenv("API_SECRET", "")
    if expected and x_api_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid API secret.")


# ── Request / response models ─────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    query: str
    member_id: str = "anonymous"


class ParlayRequest(BaseModel):
    picks: str
    member_id: str = "anonymous"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/picks/today", dependencies=[Depends(_require_secret)])
async def get_todays_picks():
    """Return the latest generated picks. Returns 404 if none generated yet today."""
    if not _latest_picks:
        raise HTTPException(status_code=404, detail="No picks generated yet. Check back after 10am ET.")
    return {
        "generated_at": _latest_picks_ts,
        "picks": _latest_picks,
    }


@router.post("/picks/generate", dependencies=[Depends(_require_secret)])
async def generate_picks_now():
    """
    Force a fresh picks generation and post to Whop feed.
    Enforces a 1-hour server-wide cooldown — daily auto-run at 10am ET
    makes this rarely needed.
    """
    global _latest_picks, _latest_picks_ts, _picks_cooldown_ts

    now = datetime.now(timezone.utc).timestamp()
    remaining = int(_PICKS_COOLDOWN - (now - _picks_cooldown_ts))
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Picks generated recently. Next refresh available in {remaining // 60}m {remaining % 60}s.",
        )

    async with _generation_lock:
        # Re-check inside lock in case of concurrent requests
        now = datetime.now(timezone.utc).timestamp()
        remaining = int(_PICKS_COOLDOWN - (now - _picks_cooldown_ts))
        if remaining > 0:
            raise HTTPException(status_code=429, detail="Already generating picks.")

        _picks_cooldown_ts = now
        try:
            picks_data, _ = await asyncio.to_thread(
                generate_picks_data, _odds_client, _claude_client
            )
            if not picks_data:
                _picks_cooldown_ts = 0  # release cooldown so they can retry
                raise HTTPException(status_code=503, detail="No games available right now.")

            await asyncio.to_thread(post_and_log, picks_data)
            _latest_picks = picks_data
            _latest_picks_ts = datetime.now(timezone.utc).isoformat()

            return {"status": "ok", "generated_at": _latest_picks_ts, "picks": picks_data}
        except HTTPException:
            raise
        except Exception as e:
            _picks_cooldown_ts = 0
            logger.exception("Error in /picks/generate")
            raise HTTPException(status_code=500, detail="Picks generation failed. Try again.")


@router.post("/analyze", dependencies=[Depends(_require_secret)])
async def analyze(req: AnalyzeRequest):
    """AI analysis on any team, matchup, or question."""
    _check_rate(req.member_id)
    try:
        all_odds = await _get_odds()
        ctx = format_odds_for_prompt(all_odds, max_chars=3000)
        result = await asyncio.to_thread(analyze_matchup, _claude_client, req.query, ctx)
        return {"analysis": result}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error in /analyze")
        raise HTTPException(status_code=503, detail="Analysis temporarily unavailable.")


@router.post("/parlay", dependencies=[Depends(_require_secret)])
async def parlay(req: ParlayRequest):
    """Analyze a parlay combination."""
    _check_rate(req.member_id)
    try:
        all_odds = await _get_odds()
        ctx = format_odds_for_prompt(all_odds, max_chars=2000)
        result = await asyncio.to_thread(analyze_parlay, _claude_client, req.picks, ctx)
        return {"analysis": result, "picks": req.picks}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error in /parlay")
        raise HTTPException(status_code=503, detail="Parlay analysis temporarily unavailable.")


@router.get("/odds", dependencies=[Depends(_require_secret)])
async def odds(team: str = ""):
    """
    Return current odds. Pass ?team=lakers to filter by team name.
    Returns all odds if team is omitted.
    """
    try:
        all_odds = await _get_odds()
        if not team:
            return {"odds": all_odds}

        needle = team.lower()
        results = []
        for sport, games in all_odds.items():
            for game in games:
                home = game.get("home_team", "").lower()
                away = game.get("away_team", "").lower()
                if needle in home or needle in away:
                    results.append({"sport": sport, "game": game})

        if not results:
            raise HTTPException(status_code=404, detail=f"No games found for '{team}' today.")
        return {"team": team, "games": results}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error in /odds")
        raise HTTPException(status_code=503, detail="Odds temporarily unavailable.")


@router.get("/record", dependencies=[Depends(_require_secret)])
async def record():
    """Return all-time and 7-day W/L record + ROI."""
    try:
        stats, weekly = await asyncio.gather(
            asyncio.to_thread(get_all_stats),
            asyncio.to_thread(get_weekly_stats),
        )
        return {"all_time": stats, "last_7_days": weekly}
    except Exception:
        logger.exception("Error in /record")
        raise HTTPException(status_code=503, detail="Record unavailable.")
