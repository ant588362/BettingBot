"""
Whop app API client.

Whop has already built the frontend. We just POST picks to their endpoint
and PATCH to grade results. That's it.

Env vars:
  WHOP_APP_URL    — base URL provided by Whop (the /api/picks endpoint)
  WHOP_APP_SECRET — the x-api-secret header value provided by Whop
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "x-api-secret": os.getenv("WHOP_APP_SECRET", ""),
        "Content-Type": "application/json",
    }


def _url() -> str:
    return os.getenv("WHOP_APP_URL", "")


def post_pick(pick: dict) -> str | None:
    """
    POST a single pick to the Whop app.
    Returns the pick ID Whop assigns, or None on failure.
    """
    payload = {
        "sport": pick.get("sport", ""),
        "matchup": pick.get("teams", ""),   # Claude uses "teams", Whop calls it "matchup"
        "pick": pick.get("pick", ""),
        "confidence": pick.get("confidence", 3),
        "analysis": pick.get("analysis", ""),
        "odds": str(pick.get("odds", "")),
        "units": pick.get("units", 1),
    }
    try:
        resp = requests.post(_url(), json=payload, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        pick_id = data.get("id")
        logger.info(f"Whop: posted pick id={pick_id}  {payload['sport']} | {payload['matchup']} | {payload['pick']}")
        return str(pick_id) if pick_id is not None else None
    except requests.RequestException as e:
        logger.error(f"Whop post_pick error: {e}")
        return None


def post_all_picks(picks_data: dict) -> dict[str, str]:
    """
    Post the full day's picks (lock + top plays + longshots).
    Returns a mapping of {teams_string: whop_pick_id} so we can store IDs for grading.
    """
    if not _url() or not os.getenv("WHOP_APP_SECRET"):
        logger.warning("WHOP_APP_URL or WHOP_APP_SECRET not set — skipping Whop post")
        return {}

    id_map: dict[str, str] = {}

    lock = picks_data.get("lock_of_the_day")
    if lock:
        pick_id = post_pick(lock)
        if pick_id:
            id_map[lock.get("teams", "")] = pick_id

    for p in picks_data.get("top_picks", []):
        pick_id = post_pick(p)
        if pick_id:
            id_map[p.get("teams", "")] = pick_id

    for p in picks_data.get("longshots", []):
        pick_id = post_pick(p)
        if pick_id:
            id_map[p.get("teams", "")] = pick_id

    logger.info(f"Whop: posted {len(id_map)} picks successfully")
    return id_map


def grade_pick(whop_id: str, result: str) -> bool:
    """
    PATCH a pick with its result.
    result must be exactly "win", "loss", or "push".
    """
    result = result.lower().strip()
    if result not in ("win", "loss", "push"):
        logger.error(f"Invalid result '{result}' — must be win, loss, or push")
        return False

    try:
        resp = requests.patch(
            _url(),
            json={"id": whop_id, "result": result},
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"Whop: graded pick {whop_id} → {result}")
        return True
    except requests.RequestException as e:
        logger.error(f"Whop grade_pick error: {e}")
        return False
